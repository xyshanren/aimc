"""Phase 3.3 (2026-07-29) — 自动入组/出组服务

消费 `promotions.raw_data._intelligence_kind == "model_metadata"` 行里的 models[]，按置信度分级：
- confidence >= 0.8 (high)  → 直接 upsert ModelRegistry + 自动加入对应 tier: 组
- confidence >= 0.6 (medium) → 写 log + 不动 ModelRegistry (等待人工确认)
- confidence < 0.6 (low)    → 写 log (标黄) + 不入 registry
- status="deprecated" 且 confidence >= 0.9 → 自动 mark ModelRegistry.status=deprecated

安全阀：
- tier 调整永远走人工确认：写 model_capabilities_log，approved=0，dashboard 批准才置 1
- capabilities / context_length / modality 变更也走 log，不直接改 ModelRegistry
- 出组（status=deprecated）只在 raw_data.status="deprecated" 且 confidence >= 0.9 时执行
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Optional

from db import crud
from db.engine import async_session_factory
from schemas.intel import ModelIntelItem

logger = logging.getLogger(__name__)

# 自动动作的置信度阈值
JOIN_THRESHOLD = 0.8
LEAVE_THRESHOLD = 0.9


@dataclass
class SyncResult:
    """单条 model 元信息的处理结果"""

    model_id: str
    action: str  # auto_joined / auto_left / pending_review / rejected / noop
    confidence: float
    target_group: Optional[str] = None
    reason: str = ""


def should_auto_join(item: ModelIntelItem) -> bool:
    """高置信度 + tier 明确 → 可入组"""
    return (
        item.confidence >= JOIN_THRESHOLD
        and item.inferred_tier is not None
        and item.family is not None
    )


def should_auto_leave(item: ModelIntelItem) -> bool:
    """AI 标记 deprecated + 极高置信度 → 可出组"""
    return (
        item.status == "deprecated"
        and item.confidence >= LEAVE_THRESHOLD
    )


async def _find_target_group(session, inferred_tier: str) -> Optional[str]:
    """按 inferred_tier 找一个 tier: 前缀的 group。

    P0-3 fix: 只有 T0 (旗舰) 可以免占路由。
    - tier:flagship 组的 tier 列为 NULL (不限档, 以 min_context=64000 兑底),
      按原来的 `g.tier == inferred_tier` 永远匹配不到 → 200B+ 旗舰 model 永远不会自动入组.
    - 修法: T0 硬编码路由到 "tier:flagship", 跳过 tier 列判断.
    - 其他档 (T1/T2/T3/T4) 不再自动入 tier:组, 改为走人工确认:
      tier 变更仍写 model_capabilities_log (approved=0), dashboard 批后由 E1 approve path
      (commit 5) 负责 DELETE + INSERT 到正确 group.
    """
    if inferred_tier == "T0":
        # flagship 组的 group_id 是定义里的硬编码 (scripts/seed_model_groups.py),
        # 不查库以免表名/列名变动导致路由脱纸.
        return "tier:flagship"
    # T1/T2/T3/T4: 等 dashboard 确认 tier log 后再路由.
    return None


async def process_model_item(
    session,
    item: ModelIntelItem,
) -> SyncResult:
    """处理单个模型元信息。

    副作用：写 model_registry / model_group_members / model_capabilities_log。
    """
    model_id = item.model_id

    # 1) 出组判定 (Phase 3.3 关键安全阀: 极高置信度)
    # P0-4 fix: must_auto_leave 一旦成立 (item.status=deprecated 且 conf>=0.9),
    # 无条件 return SyncResult, 不准 fallthrough 到下面的入组逻辑.
    # 之前只在 inner-if 里 return, 如果 existing 已是 deprecated (重复扫) 或
    # registry 没有该 model, 都会 fallthrough 跳到入组判定 → deprecated model 被重新入组.
    if should_auto_leave(item):
        existing = await crud.get_model_registry(session, model_id)
        registry_changed = False
        if existing and existing.status != "deprecated":
            old_status = existing.status
            existing.status = "deprecated"
            registry_changed = True
            await crud.log_capability_change(
                session,
                model_id=model_id,
                field_changed="status",
                old_value=old_status,
                new_value="deprecated",
                source="ai_infer",
                confidence=item.confidence,
                approved=1,  # auto_left 自动生效 (因为有 LEAVE_THRESHOLD 0.9 兜底)
            )
            logger.info(
                "自动出组: model=%s status %s -> deprecated (conf=%.2f)",
                model_id, old_status, item.confidence,
            )
        else:
            # 已 deprecated (重复扫) 或 registry 不存在 (从来没入过组)
            logger.debug(
                "出组无变化: model=%s conf=%.2f (existing=%s)",
                model_id, item.confidence,
                "deprecated" if existing else "absent",
            )

        # Phase 4 v4.1 hotfix (2026-08-06): auto_left 直接软删 group_members.
        # 之前只改 registry.status, member 仍 is_active=1, 路由 cache 可能选到.
        # 现在一步走: status=deprecated + member.is_active=0 同步完成.
        # 与 P1-2 cleanup 脚本幂等 (重复执行无副作用).
        # 幂等性保证: remove_model_group_members_by_model WHERE is_active=1 AND model_id=?
        # 已 is_active=0 的行不被重设.
        removed_count = await crud.remove_model_group_members_by_model(
            session,
            model_id,
            reason="deprecated+conf>=0.9",
            removed_by="intel_auto",
        )
        if removed_count > 0:
            logger.info(
                "auto_left 软删 group_members: model=%s 受影响 %d 条",
                model_id, removed_count,
            )

        # dashboard 提示 (不论有否变动)
        if registry_changed or removed_count > 0:
            logger.debug(
                "auto_left 闭环: model=%s registry_changed=%s members_removed=%d",
                model_id, registry_changed, removed_count,
            )

        return SyncResult(
            model_id=model_id, action="auto_left",
            confidence=item.confidence, reason="deprecated + conf>=0.9",
        )

    # 2) 入组判定 (高置信度)
    if not should_auto_join(item):
        # 写 log + 不动 registry
        await crud.log_capability_change(
            session,
            model_id=model_id,
            field_changed="metadata",
            old_value=None,
            new_value=json.dumps(item.model_dump(), ensure_ascii=False),
            source="ai_infer",
            confidence=item.confidence,
            approved=0,
        )
        action = "pending_review" if item.confidence >= 0.6 else "rejected"
        logger.debug(
            "待确认: model=%s conf=%.2f tier=%s family=%s (action=%s)",
            model_id, item.confidence, item.inferred_tier, item.family, action,
        )
        return SyncResult(
            model_id=model_id, action=action,
            confidence=item.confidence, reason="conf<tier 不达标",
        )

    # 3) 高置信度入组
    target_group = await _find_target_group(session, item.inferred_tier)
    existing = await crud.get_model_registry(session, model_id)
    if not existing:
        # 新增 registry (tier 字段保留 T2 占位, 实际 tier 走 log + 人工确认)
        await crud.upsert_model_registry(
            session,
            model_id=model_id,
            family=item.family or "other",
            tier="T2",  # 占位
            display_name=item.display_name,
            context_length=item.context_length,
            modality=item.modality or "text",
            is_open_source=1 if item.is_open_source else 0 if item.is_open_source is False else 1,
            release_date=None,
            status=item.status or "active",
            notes=f"auto-joined from intel (conf={item.confidence:.2f})",
        )
        await crud.log_capability_change(
            session,
            model_id=model_id,
            field_changed="create",
            old_value=None,
            new_value=json.dumps(item.model_dump(), ensure_ascii=False),
            source="ai_infer",
            confidence=item.confidence,
            approved=0,
        )
    else:
        # P0-5 fix: 字段变更全部走 model_capabilities_log (approved=0 等待人工),
        # 不直接 mutate existing.* (之前 context_length / modality 绕过审批,
        # 与 docstring 中 "capabilities / context_length / modality 变更也走 log" 矛盾).
        if item.display_name and existing.display_name != item.display_name:
            await crud.log_capability_change(
                session,
                model_id=model_id,
                field_changed="display_name",
                old_value=existing.display_name,
                new_value=item.display_name,
                source="ai_infer",
                confidence=item.confidence,
                approved=0,
            )
        if item.context_length and existing.context_length != item.context_length:
            await crud.log_capability_change(
                session,
                model_id=model_id,
                field_changed="context_length",
                old_value=existing.context_length,
                new_value=item.context_length,
                source="ai_infer",
                confidence=item.confidence,
                approved=0,
            )
        if item.modality and existing.modality != item.modality:
            await crud.log_capability_change(
                session,
                model_id=model_id,
                field_changed="modality",
                old_value=existing.modality,
                new_value=item.modality,
                source="ai_infer",
                confidence=item.confidence,
                approved=0,
            )

    # tier 写 log (P1-4 fix: 仅当新建 或 tier 变动时才写, 避免重复入组刷 tier log 噪声)
    if not existing or existing.tier != item.inferred_tier:
        await crud.log_capability_change(
            session,
            model_id=model_id,
            field_changed="tier",
            old_value=existing.tier if existing else None,
            new_value=item.inferred_tier,
            source="ai_infer",
            confidence=item.confidence,
            approved=0,  # 永远人工确认
        )

    # 自动入组 (仅当找到对应 tier: 组)
    if target_group:
        joined = await crud.add_model_group_member(
            session, target_group, model_id,
            priority=100, weight=1.0, added_by="intel_auto",
        )
        if joined:
            logger.info(
                "自动入组: model=%s -> group=%s (conf=%.2f)",
                model_id, target_group, item.confidence,
            )
    else:
        logger.debug(
            "未找到 tier=%s 对应组, model=%s 暂不入组",
            item.inferred_tier, model_id,
        )

    return SyncResult(
        model_id=model_id, action="auto_joined",
        confidence=item.confidence, target_group=target_group,
    )


async def process_promotion(
    promotion_id: int,
    raw_data_json: str,
) -> list[SyncResult]:
    """处理一条 promotions 行 (Phase 3.3 入口)。

    raw_data_json 应含 _intelligence_kind == "model_metadata" 与 models[]。
    返回每条 model 的 SyncResult。
    """
    if not raw_data_json:
        return []
    try:
        data = json.loads(raw_data_json)
    except (ValueError, TypeError):
        logger.warning("promotion %s raw_data JSON 解析失败", promotion_id)
        return []
    if data.get("_intelligence_kind") != "model_metadata":
        return []
    models = data.get("models") or []
    if not models:
        return []

    results: list[SyncResult] = []
    async with async_session_factory() as session:
        for raw in models:
            # 1) Pydantic 验证是 sync 阶段, 失败 skip 后续不写表
            try:
                item = ModelIntelItem(**raw)
            except Exception as e:
                logger.warning("跳过无效 model 项: %s", e)
                continue
            # 2) P1-3 fix: 每个 model 独立 SAVEPOINT (begin_nested).
            # 原实现整个 for 循环包在一个 transaction 里, 任何一条 model 报
            # SQLAlchemy 异常 (e.g. IntegrityError, OperationalError) 都会
            # 污染 session 状态, 外层 commit 一起 rollback 掉所有前面的 model.
            # 改为 savepoint 后, 单条失败只 rollback 该 savepoint, 后续 model
            # 继续处理, 成功的正常 commit.
            try:
                async with session.begin_nested():
                    result = await process_model_item(session, item)
                results.append(result)
            except Exception as e:
                logger.error(
                    "model %s 处理失败 (savepoint rollback, 继续下一条): %s",
                    item.model_id, e,
                )
                # 关键: SQLAlchemy 异常后 session 进入 "errored" 状态, 必须 rollback
                # 但 begin_nested() 自动 rollback 了 savepoint, 主事务还在.
                # 接下来 next iteration 仍可正常使用 session.
        await session.commit()
    return results
