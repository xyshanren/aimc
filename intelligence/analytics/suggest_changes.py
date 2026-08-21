"""方案 A (2026-08-14): 模型 / 分组调整建议生成器

背景:
- model_group_members 静态, 模型升级 (e.g. GLM-5.3) 需要手工改
- 4 个数据源已有 (model_cards / usage_logs / price_snapshots / cache usage_logs)
- 本模块综合分析, 写入 model_change_suggestions / group_change_suggestions 表

设计:
- 4 个 model 规则 (tier 升 / tier 降 / 加进 group / 移除)
- 4 个 group 规则 (创建 / 合并 / 删除 / 重命名)
- 置信度 4 数据源加权 (model_cards score / usage 稳定性 / price 稳定性 / cache 命中率)
- --shadow-mode flag: 写 auto_apply_shadow.log 而不写 db (远期自动应用决策数据)
- --apply flag: 直接写 db (生产模式)

最小数据门槛: usage_logs >= 30 条 AND model 种类 >= 3 (避免冷启动猜)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# 项目根目录 (intelligence/analytics/suggest_changes.py 上两级)
ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from db.engine import async_session_factory
from db.init_db import init_db
from db.models import (
    AccountQuota, AccountQuotaUsage, Channel, ModelCard,
    ModelChangeSuggestion, ModelGroup, ModelGroupMember,
    ModelRegistry, GroupChangeSuggestion, PriceSnapshot,
    UsageLog,
)

logger = logging.getLogger(__name__)


# 置信度权重 (后续 dashboard 可调)
WEIGHT_SCORE_SIMILARITY = 0.5   # model_cards score 跟 tier 平均匹配度
WEIGHT_USAGE_STABILITY = 0.3    # usage_logs 最近 N 天稳定性
WEIGHT_PRICE_STABILITY = 0.1    # price_snapshots 价格稳定性
WEIGHT_CACHE_HIT_RATE = 0.1     # usage_logs.cached_input_tokens 命中率

# 最小数据门槛
MIN_USAGE_LOGS = 30
MIN_DISTINCT_MODELS = 3


def compute_confidence(
    score_similarity: float,
    usage_stability: float,
    price_stability: float,
    cache_hit_rate: float,
) -> float:
    """综合 4 数据源置信度 (0-1)"""
    return (
        WEIGHT_SCORE_SIMILARITY * score_similarity
        + WEIGHT_USAGE_STABILITY * usage_stability
        + WEIGHT_PRICE_STABILITY * price_stability
        + WEIGHT_CACHE_HIT_RATE * cache_hit_rate
    )


def score_similarity(new_tier: str, model_score: float, tier_avg_score: float) -> float:
    """score 跟 tier 平均的匹配度 (0-1)
    - 1.0: 完全匹配 (model_score == tier_avg_score)
    - 0.5: 差 50%
    - 0.0: 差 100%+
    """
    if tier_avg_score <= 0:
        return 0.5
    diff = abs(model_score - tier_avg_score) / tier_avg_score
    return max(0.0, 1.0 - diff)


def usage_stability(recent_count: int, max_recent_count: int) -> float:
    """使用率稳定性 (0-1)
    - 1.0: 最近使用次数跟 max 一致 (稳定使用)
    - 0.0: 0 使用 (可能下线)
    """
    if max_recent_count <= 0:
        return 0.0
    return min(1.0, recent_count / max_recent_count)


def price_stability(prices: list[float]) -> float:
    """价格稳定性 (0-1, stddev/avg 越小越稳)"""
    if not prices or len(prices) < 2:
        return 0.5  # 数据不够, 给中等
    avg = sum(prices) / len(prices)
    if avg <= 0:
        return 0.0
    variance = sum((p - avg) ** 2 for p in prices) / len(prices)
    stddev = variance ** 0.5
    cv = stddev / avg
    return max(0.0, 1.0 - min(cv, 1.0))


async def check_min_data(session: AsyncSession) -> bool:
    """检查是否满足最小数据门槛"""
    n = (await session.execute(select(func.count(UsageLog.id)))).scalar() or 0
    if n < MIN_USAGE_LOGS:
        logger.info(f"usage_logs 只有 {n} 行 (<{MIN_USAGE_LOGS}), 跳过")
        return False
    n_models = (await session.execute(
        select(func.count(func.distinct(UsageLog.model)))
    )).scalar() or 0
    if n_models < MIN_DISTINCT_MODELS:
        logger.info(f"只有 {n_models} 种 model (<{MIN_DISTINCT_MODELS}), 跳过")
        return False
    return True


# ============================================================
# Model 规则
# ============================================================

async def rule_tier_change(session: AsyncSession) -> list[ModelChangeSuggestion]:
    """规则 1+2: tier 升降
    - 升: model_score > next_tier_avg * 0.95
    - 降: model_score < current_tier_avg * 0.7 (过度配置)
    """
    suggestions = []
    # 当前每个 tier 平均 score
    tier_avg_stmt = (
        select(ModelRegistry.tier, func.avg(ModelCard.score).label("avg_score"))
        .join(ModelCard, ModelRegistry.model_id == ModelCard.model, isouter=True)
        .where(ModelRegistry.tier.isnot(None), ModelRegistry.status == "active")
        .group_by(ModelRegistry.tier)
    )
    tier_avg_rows = (await session.execute(tier_avg_stmt)).all()
    tier_avg = {r.tier: float(r.avg_score or 0) for r in tier_avg_rows}

    # 所有 active model + score
    model_stmt = (
        select(ModelRegistry.model_id, ModelRegistry.tier, ModelCard.score)
        .join(ModelCard, ModelRegistry.model_id == ModelCard.model, isouter=True)
        .where(ModelRegistry.status == "active")
    )
    model_rows = (await session.execute(model_stmt)).all()

    tier_order = ["T0", "T1", "T2", "T3", "T4"]

    for row in model_rows:
        model_id = row.model_id
        current_tier = row.tier
        score = float(row.score or 0)
        if not current_tier or score <= 0:
            continue
        cur_avg = tier_avg.get(current_tier, 0)
        if cur_avg <= 0:
            continue

        # 检查降
        if score < cur_avg * 0.7:
            sim = score_similarity(current_tier, score, cur_avg)
            # usage stability (简化: 当前 tier 用得越多越稳)
            n_use = (await session.execute(
                select(func.count(UsageLog.id)).where(
                    UsageLog.model == model_id,
                    UsageLog.created_at >= datetime.now() - timedelta(days=7)
                )
            )).scalar() or 0
            max_use = 100  # 简化兜底
            us = usage_stability(n_use, max_use)
            conf = compute_confidence(sim, us, 0.5, 0.0)
            suggestions.append(ModelChangeSuggestion(
                model_id=model_id,
                change_type="tier_change",
                suggested_value=_suggest_lower_tier(score, tier_order, tier_avg),
                current_value=current_tier,
                reason=f"评分 {score:.0f} 远低于当前 tier 平均 {cur_avg:.0f} (<70%), 建议降级",
                confidence=conf,
                based_on=json.dumps({
                    "score": score, "current_tier": current_tier,
                    "current_tier_avg": cur_avg, "rule": "tier_down",
                }),
            ))
            continue

        # 检查升 (找下一个更高 tier)
        idx = tier_order.index(current_tier) if current_tier in tier_order else -1
        if idx < 0 or idx == 0:
            continue
        higher_tier = tier_order[idx - 1]  # tier_order 越靠前越高
        higher_avg = tier_avg.get(higher_tier, 0)
        if higher_avg > 0 and score >= higher_avg * 0.95:
            sim = score_similarity(higher_tier, score, higher_avg)
            n_use = (await session.execute(
                select(func.count(UsageLog.id)).where(
                    UsageLog.model == model_id,
                    UsageLog.created_at >= datetime.now() - timedelta(days=7)
                )
            )).scalar() or 0
            us = usage_stability(n_use, 100)
            conf = compute_confidence(sim, us, 0.5, 0.0)
            suggestions.append(ModelChangeSuggestion(
                model_id=model_id,
                change_type="tier_change",
                suggested_value=higher_tier,
                current_value=current_tier,
                reason=f"评分 {score:.0f} 已达到 {higher_tier} 平均 {higher_avg:.0f} (>=95%), 建议升级",
                confidence=conf,
                based_on=json.dumps({
                    "score": score, "current_tier": current_tier,
                    "higher_tier": higher_tier, "higher_tier_avg": higher_avg,
                    "rule": "tier_up",
                }),
            ))
    return suggestions


def _suggest_lower_tier(score: float, tier_order, tier_avg) -> str:
    """建议降到的 tier (score 下一个低 tier)"""
    for t in tier_order:  # T0 -> T4
        if t in tier_avg and score < tier_avg[t]:
            return t
    return tier_order[-1]


async def rule_add_to_group(session: AsyncSession) -> list[ModelChangeSuggestion]:
    """规则 3: 应该加进新 group (model score 跟 group top member 持平, 但不在 group 里)"""
    suggestions = []
    # 所有 group + 它们的 members score
    group_stmt = (
        select(ModelGroup.group_id, ModelGroupMember.model_id, ModelCard.score)
        .join(ModelGroupMember, ModelGroup.id == ModelGroupMember.group_id)
        .join(ModelCard, ModelGroupMember.model_id == ModelCard.model, isouter=True)
        .where(ModelGroupMember.is_active == 1)
    )
    group_rows = (await session.execute(group_stmt)).all()
    from collections import defaultdict
    group_models: dict[str, list] = defaultdict(list)
    for r in group_rows:
        group_models[r.group_id].append((r.model_id, float(r.score or 0)))

    # 计算每个 group 的 top-1 score (threshold = 加入门槛)
    group_top_score = {g: max((s for _, s in ms), default=0) for g, ms in group_models.items()}

    # 所有 active models
    all_models_stmt = select(ModelRegistry.model_id, ModelCard.score).join(
        ModelCard, ModelRegistry.model_id == ModelCard.model, isouter=True
    ).where(ModelRegistry.status == "active")
    all_models = (await session.execute(all_models_stmt)).all()

    for row in all_models:
        model_id = row.model_id
        score = float(row.score or 0)
        if score <= 0:
            continue
        for gid, top in group_top_score.items():
            if top <= 0 or score < top * 0.95:
                continue
            # 检查是否已在 group
            existing = (await session.execute(
                select(func.count(ModelGroupMember.id)).where(
                    ModelGroupMember.model_id == model_id,
                    ModelGroupMember.is_active == 1,
                ).join(ModelGroup, ModelGroup.id == ModelGroupMember.group_id).where(
                    ModelGroup.group_id == gid
                )
            )).scalar() or 0
            if existing > 0:
                continue  # 已在
            # 建议加入
            sim = score_similarity(gid, score, top)
            conf = compute_confidence(sim, 0.6, 0.5, 0.0)
            suggestions.append(ModelChangeSuggestion(
                model_id=model_id,
                change_type="add_to_group",
                suggested_value=gid,
                current_value=None,
                reason=f"评分 {score:.0f} 接近 {gid} top member ({top:.0f}), 建议加入",
                confidence=conf,
                based_on=json.dumps({
                    "model": model_id, "group": gid,
                    "model_score": score, "top_score": top,
                    "rule": "add_to_group",
                }),
            ))
    return suggestions


async def rule_remove_from_group(session: AsyncSession) -> list[ModelChangeSuggestion]:
    """规则 4: 应该从 group 移除 (长期不用, e.g. 30 天 0 使用)"""
    suggestions = []
    cutoff = datetime.now() - timedelta(days=30)
    stmt = (
        select(ModelGroup.group_id, ModelGroupMember.model_id)
        .join(ModelGroup, ModelGroup.id == ModelGroupMember.group_id)
        .where(ModelGroupMember.is_active == 1)
    )
    member_rows = (await session.execute(stmt)).all()

    for r in member_rows:
        gid, model_id = r.group_id, r.model_id
        n_use = (await session.execute(
            select(func.count(UsageLog.id)).where(
                UsageLog.model == model_id,
                UsageLog.created_at >= cutoff,
            )
        )).scalar() or 0
        if n_use > 0:
            continue
        conf = compute_confidence(0.0, 0.0, 0.5, 0.0)
        suggestions.append(ModelChangeSuggestion(
            model_id=model_id,
            change_type="remove_from_group",
            suggested_value=gid,
            current_value=gid,
            reason=f"30 天 0 次请求, 建议从 {gid} 移除",
            confidence=conf,
            based_on=json.dumps({
                "model": model_id, "group": gid, "days_idle": 30,
                "rule": "remove_idle",
            }),
        ))
    return suggestions


# ============================================================
# Group 规则
# ============================================================

async def rule_merge_groups(session: AsyncSession) -> list[GroupChangeSuggestion]:
    """规则 1: 合并低使用率 group (近 7 天 < 5 次请求)"""
    suggestions = []
    cutoff = datetime.now() - timedelta(days=7)
    stmt = (
        select(ModelGroup.group_id, func.count(UsageLog.id).label("n"))
        .outerjoin(
            ModelGroupMember,
            ModelGroup.id == ModelGroupMember.group_id,
        )
        .outerjoin(UsageLog, UsageLog.model == ModelGroupMember.model_id)
        .where(UsageLog.created_at >= cutoff)
        .group_by(ModelGroup.group_id)
    )
    rows = (await session.execute(stmt)).all()
    # 找低使用率 group (排除 tier:flagship 等核心)
    protected = {"tier:flagship", "tier:vision", "tier:code"}
    for r in rows:
        if r.group_id in protected:
            continue
        if r.n < 5 and r.n >= 0:
            # 找 fallback_group 链里的下一个 (合并目标)
            target = await _find_merge_target(session, r.group_id)
            if not target:
                continue
            conf = compute_confidence(0.0, 0.1, 0.5, 0.0)
            suggestions.append(GroupChangeSuggestion(
                group_id=r.group_id,
                change_type="merge",
                target_groups=f"{r.group_id},{target}",
                suggested_config=None,
                reason=f"7 天只 {r.n} 次请求, 建议合并进 {target}",
                confidence=conf,
                based_on=json.dumps({
                    "group": r.group_id, "recent_7d_requests": r.n,
                    "merge_target": target, "rule": "merge_low_usage",
                }),
            ))
    return suggestions


async def _find_merge_target(session: AsyncSession, group_id: str) -> Optional[str]:
    """找合并目标: 优先 fallback_group, 否则按 tier 顺序"""
    g = (await session.execute(
        select(ModelGroup).where(ModelGroup.group_id == group_id)
    )).scalars().first()
    if g and g.fallback_group:
        return g.fallback_group
    # 兜底: 找一个 tier 相近的 group
    return None


async def rule_delete_groups(session: AsyncSession) -> list[GroupChangeSuggestion]:
    """规则 2: 删除 0 使用率 group (30 天 0 请求)"""
    suggestions = []
    cutoff = datetime.now() - timedelta(days=30)
    protected = {"tier:flagship", "tier:vision", "tier:code"}
    stmt = (
        select(ModelGroup.group_id)
        .outerjoin(ModelGroupMember, ModelGroup.id == ModelGroupMember.group_id)
        .outerjoin(UsageLog, UsageLog.model == ModelGroupMember.model_id)
        .where(UsageLog.created_at >= cutoff)
        .group_by(ModelGroup.group_id)
        .having(func.count(UsageLog.id) == 0)
    )
    rows = (await session.execute(stmt)).all()
    for r in rows:
        if r.group_id in protected:
            continue
        conf = compute_confidence(0.0, 0.0, 0.5, 0.0)
        suggestions.append(GroupChangeSuggestion(
            group_id=r.group_id,
            change_type="delete",
            target_groups=None,
            reason=f"30 天 0 次请求, 建议删除",
            confidence=conf,
            based_on=json.dumps({
                "group": r.group_id, "days_idle": 30, "rule": "delete_zero_usage",
            }),
        ))
    return suggestions


async def rule_new_groups(session: AsyncSession) -> list[GroupChangeSuggestion]:
    """规则 3: 新增 group (fallback chain 走完整 503 比例 > 5%)"""
    # 暂不实现 (需要 usage status_code 聚合, 数据有限)
    return []


async def rule_rename_groups(session: AsyncSession) -> list[GroupChangeSuggestion]:
    """规则 4: 重命名 (group 名字暗示 tier 但实际 model score 不匹配)"""
    # 暂不实现 (复杂, 需要 name parser)
    return []


# ============================================================
# 主入口
# ============================================================

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="直接写 db (生产)")
    parser.add_argument("--shadow-mode", action="store_true",
                        help="影子模式: 写 log, 不写 db (远期决策数据积累)")
    args = parser.parse_args()

    if not args.apply and not args.shadow_mode:
        args.shadow_mode = True  # 默认 shadow mode 安全

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    await init_db()
    async with async_session_factory() as session:
        # 检查最小数据门槛
        if not await check_min_data(session):
            logger.info("不满足最小数据门槛, 跳过")
            return

        # 收集所有建议
        all_model_suggestions = []
        all_group_suggestions = []

        logger.info("=== Model 规则 ===")
        for fn in (rule_tier_change, rule_add_to_group, rule_remove_from_group):
            try:
                results = await fn(session)
                logger.info(f"  {fn.__name__}: {len(results)} 条")
                all_model_suggestions.extend(results)
            except Exception as e:
                logger.error(f"  {fn.__name__} failed: {e}")

        logger.info("=== Group 规则 ===")
        for fn in (rule_merge_groups, rule_delete_groups, rule_new_groups, rule_rename_groups):
            try:
                results = await fn(session)
                logger.info(f"  {fn.__name__}: {len(results)} 条")
                all_group_suggestions.extend(results)
            except Exception as e:
                logger.error(f"  {fn.__name__} failed: {e}")

        logger.info(f"=== 总计: {len(all_model_suggestions)} model + {len(all_group_suggestions)} group ===")

        if args.shadow_mode:
            # 写 shadow log
            shadow_path = Path("/home/lxc/aimc/intelligence/auto_apply_shadow.log")
            try:
                with open(shadow_path, "a") as f:
                    f.write(f"\n=== {datetime.now().isoformat()} ===\n")
                    f.write(f"Model suggestions ({len(all_model_suggestions)}):\n")
                    for s in all_model_suggestions:
                        f.write(f"  {s.change_type} {s.model_id}: {s.current_value} -> {s.suggested_value} "
                                f"(conf={s.confidence:.2f}) {s.reason}\n")
                    f.write(f"Group suggestions ({len(all_group_suggestions)}):\n")
                    for s in all_group_suggestions:
                        f.write(f"  {s.change_type} {s.group_id} -> {s.target_groups} "
                                f"(conf={s.confidence:.2f}) {s.reason}\n")
                logger.info(f"[shadow] 已写 {shadow_path}")
            except Exception as e:
                logger.error(f"[shadow] 写 log 失败: {e}")

        if args.apply:
            # 清旧 pending + 写新 pending
            await session.execute(
                delete(ModelChangeSuggestion).where(ModelChangeSuggestion.status == "pending")
            )
            await session.execute(
                delete(GroupChangeSuggestion).where(GroupChangeSuggestion.status == "pending")
            )
            for s in all_model_suggestions:
                session.add(s)
            for s in all_group_suggestions:
                session.add(s)
            await session.commit()
            logger.info(f"[apply] 写入 {len(all_model_suggestions)} model + {len(all_group_suggestions)} group 建议")


if __name__ == "__main__":
    asyncio.run(main())
