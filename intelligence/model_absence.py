"""模型消失检测 (2026-10-15)

背景: 公告型源 (Playwright) 全部下线后, intel_auto 的 deprecated 判定失去
输入源 — 模型下线只能靠真实流量打上游 400 才被发现。本模块用
「上游 /v1/models 列表对比」补上这个触发器:

    model_list 源每次成功拉取 → 与 model_list_snapshots 快照对比 →
    模型连续 2 次同步未出现 (约 2 天) → remove_from_group suggestion
    (走既有建议卡人工确认; 与 intel_auto 护栏同语义 — 不自动出组)

防抖设计:
1. 列变小守卫: 本次列表规模 < 上次快照 50% → 跳过本轮 (上游分页/故障)
2. 连续缺失: miss_count >= 2 才触发; 模型重新出现即清零
3. 只对「有活跃组成员」的模型建 suggestion (无组员的无从移除,
   价格行残留靠看板 NULL 价格盘点卡人工处理)

消费方: intelligence/scheduler.py 的 model_list API 成功分支。
"""

import json
import logging
from datetime import datetime

from sqlalchemy import select

from db.engine import async_session_factory
from db.models import ModelChangeSuggestion, ModelGroupMember, ModelListSnapshot

logger = logging.getLogger(__name__)

# 连续缺失多少次同步才触发 suggestion (model_list 源 poll_interval=1 天 → 约 2 天)
MISS_THRESHOLD = 2
# 列变小守卫: 本次 / 上次 < 该比例 → 视为上游异常, 跳过检测
SHRINK_GUARD_RATIO = 0.5


async def detect_missing_models(provider: str, current_models: list) -> dict:
    """对比快照检测消失模型并维护快照表 (每次成功拉取后调用)

    Returns: {"tracked": N, "shrunk_skipped": bool, "missing_now": [...],
              "suggestions_created": [...], "returned": [...]}
    """
    now = datetime.now()
    current = set(current_models)
    stats = {"tracked": 0, "shrunk_skipped": False,
             "missing_now": [], "suggestions_created": [], "returned": []}

    async with async_session_factory() as session:
        snapshots = list((await session.execute(
            select(ModelListSnapshot).where(ModelListSnapshot.provider == provider)
        )).scalars().all())
        prev_models = {s.model for s in snapshots}

        # 防抖 1: 列明显变小 → 上游异常, 只 log 不检测 (快照不动)
        if prev_models and len(current) < len(prev_models) * SHRINK_GUARD_RATIO:
            logger.warning(
                f"[model-absence] {provider} 列表规模骤降 "
                f"{len(prev_models)} -> {len(current)} (<{SHRINK_GUARD_RATIO:.0%}), "
                f"跳过本轮消失检测 (疑上游分页/故障)"
            )
            stats["shrunk_skipped"] = True
            return stats

        # 活跃组员索引 (只为有组员的模型建 suggestion)
        active_members = set((await session.execute(
            select(ModelGroupMember.model_id).where(ModelGroupMember.is_active == 1)
        )).scalars().all())

        by_model = {s.model: s for s in snapshots}

        # 1) 消失的: miss_count += 1, 达阈值且有组员 → suggestion
        for s in snapshots:
            if s.model in current:
                continue
            s.miss_count += 1
            s.last_seen_at = s.last_seen_at  # 保持最后出现时间不变
            if s.miss_count >= MISS_THRESHOLD:
                stats["missing_now"].append(s.model)
                if s.model in active_members:
                    created = await _ensure_absence_suggestion(
                        session, provider, s.model)
                    if created:
                        stats["suggestions_created"].append(s.model)
                else:
                    logger.info(
                        f"[model-absence] {provider}/{s.model} 连续 "
                        f"{s.miss_count} 次未出现 (无活跃组员, 仅快照记录)"
                    )

        # 2) 仍在列表的: miss 清零 + last_seen 更新
        # 3) 新出现的: 插入快照
        for m in current:
            s = by_model.get(m)
            if s is None:
                session.add(ModelListSnapshot(
                    provider=provider, model=m,
                    last_seen_at=now, miss_count=0))
            else:
                s.miss_count = 0
                s.last_seen_at = now

        stats["tracked"] = len(prev_models)
        await session.commit()

    if stats["missing_now"]:
        logger.info(
            f"[model-absence] {provider}: 消失待确认 {stats['missing_now']}, "
            f"新建 suggestion {stats['suggestions_created']}"
        )
    return stats


async def _ensure_absence_suggestion(session, provider: str, model_id: str) -> bool:
    """幂等创建 remove_from_group suggestion (来源=上游列表消失); 已有 pending 跳过"""
    existing = (await session.execute(
        select(ModelChangeSuggestion.id).where(
            ModelChangeSuggestion.model_id == model_id,
            ModelChangeSuggestion.change_type == "remove_from_group",
            ModelChangeSuggestion.status == "pending",
        ).limit(1)
    )).scalar_one_or_none()
    if existing is not None:
        return False

    groups = list((await session.execute(
        select(ModelGroupMember.group_id).where(
            ModelGroupMember.model_id == model_id,
            ModelGroupMember.is_active == 1)
    )).scalars().all())
    if not groups:
        return False  # 双保险 (调用方已查过 active_members)

    groups_str = ", ".join(groups)
    session.add(ModelChangeSuggestion(
        model_id=model_id,
        change_type="remove_from_group",
        suggested_value=groups[0],
        current_value=groups_str,
        reason=(
            f"模型消失检测: {provider} 上游模型列表连续 {MISS_THRESHOLD} 次同步"
            f"未出现该模型 (大概率已下线); 活跃组=[{groups_str}]. "
            f"批准后出组; 价格行请一并人工核对。"
        ),
        confidence=0.9,
        based_on=json.dumps({
            "model": model_id,
            "rule": "upstream_model_missing",
            "provider": provider,
            "miss_count": MISS_THRESHOLD,
            "active_groups": groups,
        }, ensure_ascii=False),
    ))
    await session.flush()
    logger.info(f"[model-absence] 新建出组建议: {model_id} (来自 {provider} 列表消失)")
    return True
