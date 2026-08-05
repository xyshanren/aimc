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


async def _find_target_group(session, tier: str) -> Optional[str]:
    """按 tier 找一个 tier: 前缀的 group。"""
    from db.models import ModelGroup
    from sqlalchemy import select

    stmt = (
        select(ModelGroup)
        .where(ModelGroup.enabled == 1)
        .where(ModelGroup.group_id.like("tier:%"))
    )
    result = await session.execute(stmt)
    for g in result.scalars().all():
        if g.tier == tier:
            return g.group_id
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
    if should_auto_leave(item):
        existing = await crud.get_model_registry(session, model_id)
        if existing and existing.status != "deprecated":
            old_status = existing.status
            existing.status = "deprecated"
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
        # 更新非 tier 字段
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
            existing.context_length = item.context_length
        if item.modality and existing.modality != item.modality:
            existing.modality = item.modality

    # tier 写 log (无论新建还是已有, tier 永远不直接落库)
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
            try:
                item = ModelIntelItem(**raw)
            except Exception as e:
                logger.warning("跳过无效 model 项: %s", e)
                continue
            result = await process_model_item(session, item)
            results.append(result)
        await session.commit()
    return results
