"""Phase 3.3 (2026-07-29) — 自动入组/出组服务

消费 `promotions.raw_data._intelligence_kind == "model_metadata"` 行里的 models[]，按置信度分级：
- confidence >= 0.8 (high)  → 直接 upsert ModelRegistry + 自动加入对应 tier: 组
- confidence >= 0.6 (medium) → 写 log + 不动 ModelRegistry (等待人工确认)
- confidence < 0.6 (low)    → 写 log (标黄) + 不入 registry
- status="deprecated" 且 confidence >= 0.9 → 自动出组, 但有渠道供应护栏 (2026-10-12):
  仍有 active 渠道价行供应的模型不自动出组 (registry 不标 deprecated、组员不软删),
  降级为 remove_from_group suggestion + pending capability log 待人工确认;
  无渠道供应才走原全自动路径 (registry deprecated + 组员软删).

安全阀：
- tier 调整永远走人工确认：写 model_capabilities_log，approved=0，dashboard 批准才置 1
- capabilities / context_length / modality 变更也走 log，不直接改 ModelRegistry
- 出组（status=deprecated）只在 raw_data.status="deprecated" 且 confidence >= 0.9 时执行
- 护栏（2026-10-12, v4-flash 教训）: 9/28 sensenova/taotoken 仍 0 元供应 deepseek-v4-flash,
  intel_auto 一步软删其 tier:strong/code 组员行, 组员要事后人工恢复 — 现在"渠道仍供应"
  的 deprecated 模型改走建议中心人工确认
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Optional

from db import crud
from db.engine import async_session_factory
from intelligence.model_resolver import (
    Resolution,
    is_suppliable_by_any_channel,
    resolve_canonical_model,
)
from schemas.intel import ModelIntelItem

logger = logging.getLogger(__name__)

# 自动动作的置信度阈值
JOIN_THRESHOLD = 0.8
LEAVE_THRESHOLD = 0.9

# O1 (2026-11-04): 供给侧闸门 — 本站没有任何供应证据 (无价行 / 无上游列表 / 无该厂商
# 渠道) 的模型不进待确认队列 (实证: volcengine 文档页上的 Seedream / Seedance 等
# 只有名字的 metadata 把队列刷成噪声)。单元测试用 mock session 时没有供给证据可查,
# 会整体降级 → 那些用例显式把本开关置 False (见 test_phase3_3 / test_phase3_5)。
SUPPLY_GATE_ENABLED = True

# Phase 4 v4.3.1 (2026-08-13): T1/T2/T3 也可自动入组 (默认关闭, 跟现有 T0-only 默认行为兼容)
# V6.4 S3 (2026-09-11): F3 观察期收口, 用户拍板 S3 — 开启 T1-T3 自动入组但加置信门槛.
# 依据: pending 恒 0 无审批负担 / 组命中 96.5% / 盲区 18.2% 良性 / kimi-k2.6 两次
# 推断不一致 (T1→T2) 支持 confidence 门槛方向. 门槛不满足 → 走 pending 人工路径.
AUTO_JOIN_NON_T0 = True
AUTO_JOIN_MIN_CONFIDENCE = 0.9  # 仅约束非 T0 自动入组; T0 恒免人工不受影响


# Phase 4 v4.3.1 (2026-08-13): tier -> target_group 映射
_TIER_TO_GROUP: dict[str, str] = {
    "T0": "tier:flagship",
    "T1": "tier:strong",
    "T2": "tier:balanced",
    "T3": "tier:light",
}


@dataclass
class SyncResult:
    """单条 model 元信息的处理结果"""

    model_id: str
    # auto_joined / auto_left / pending_review / rejected / noop
    # 2026-11-04 (O1): + unsupplied (本站无供应证据, 未入队)
    #                  + resolved   (已解析到现网模型, 未入队)
    action: str
    confidence: float
    target_group: Optional[str] = None
    reason: str = ""


def should_auto_join(item: ModelIntelItem) -> bool:
    """高置信度 + tier 明确 → 可入组

    V6.4 S3 (2026-09-11): 非 T0 自动入组额外要求 confidence >= AUTO_JOIN_MIN_CONFIDENCE
    (0.9); T0 恒免人工不受门槛约束.
    """
    meets_join = item.confidence >= JOIN_THRESHOLD
    if item.inferred_tier != "T0":
        meets_join = meets_join and item.confidence >= AUTO_JOIN_MIN_CONFIDENCE
    return (
        meets_join
        and item.inferred_tier is not None
        and item.family is not None
    )


def should_auto_leave(item: ModelIntelItem) -> bool:
    """AI 标记 deprecated + 极高置信度 → 可出组"""
    return (
        item.status == "deprecated"
        and item.confidence >= LEAVE_THRESHOLD
    )


async def _guardrail_active_groups(session, model_id: str) -> list[str]:
    """查模型当前所有活跃组 (护栏用).

    join 两边都是 TEXT group_id (组名), 不要 join model_groups.id (整型).
    """
    from sqlalchemy import select as _select
    from db.models import ModelGroup as _MG, ModelGroupMember as _MGM
    rows = await session.execute(
        _select(_MG.group_id)
        .join(_MGM, _MGM.group_id == _MG.group_id)
        .where(_MGM.model_id == model_id, _MGM.is_active == 1)
    )
    return list(rows.scalars().all())


async def _ensure_remove_suggestion(
    session, model_id: str, groups: list[str], item: ModelIntelItem,
) -> bool:
    """幂等创建 remove_from_group suggestion; 已有 pending 时跳过返回 False."""
    from sqlalchemy import select as _select
    from db.models import ModelChangeSuggestion as _MCS
    existing = (
        await session.execute(
            _select(_MCS)
            .where(
                _MCS.model_id == model_id,
                _MCS.change_type == "remove_from_group",
                _MCS.status == "pending",
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return False
    groups_str = ", ".join(groups) if groups else "(无活跃组)"
    session.add(_MCS(
        model_id=model_id,
        change_type="remove_from_group",
        suggested_value=groups[0] if groups else None,
        current_value=groups_str,
        reason=(
            f"intel_auto 护栏: AI 判定 deprecated (conf={item.confidence:.2f}) "
            f"但渠道仍有价行供应, 不自动出组; 活跃组=[{groups_str}]. "
            "确认要出组请在 dashboard 组管理手动移除后标记本条."
        ),
        confidence=item.confidence,
        based_on=json.dumps({
            "model": model_id,
            "rule": "intel_auto_guardrail",
            "channel_still_supplies": True,
            "active_groups": list(groups),
        }, ensure_ascii=False),
    ))
    await session.flush()
    return True


async def _find_target_group(session, inferred_tier: str) -> Optional[str]:
    """按 inferred_tier 找一个 tier: 前缀的 group。

    P0-3 fix: 只有 T0 (旗舰) 可以免占路由。
    - tier:flagship 组的 tier 列为 NULL (不限档, 以 min_context=64000 兑底),
      按原来的 `g.tier == inferred_tier` 永远匹配不到 → 200B+ 旗舰 model 永远不会自动入组.
    - 修法: T0 硬编码路由到 "tier:flagship", 跳过 tier 列判断.
    - 其他档 (T1/T2/T3/T4) 不再自动入 tier:组, 改为走人工确认:
      tier 变更仍写 model_capabilities_log (approved=0), dashboard 批后由 E1 approve path
      (commit 5) 负责 DELETE + INSERT 到正确 group.

    Phase 4 v4.3.1 (2026-08-13): 当 AUTO_JOIN_NON_T0=True 时,
    T1/T2/T3 也会自动入组 (跟 _TIER_TO_GROUP 映射).
    T4 仍不入组 (需人工).

    V6.4 S3 (2026-09-11): AUTO_JOIN_NON_T0=True 已生效 + confidence 门槛.
    注意: confidence 门槛在 should_auto_join 处判定 (process_model_item 的
    入组前置), 本函数只做 tier→group 映射, 不重复判置信度.
    """
    if inferred_tier == "T0":
        # flagship 组的 group_id 是定义里的硬编码 (scripts/seed_model_groups.py),
        # 不查库以免表名/列名变动导致路由脱纸.
        return "tier:flagship"
    if AUTO_JOIN_NON_T0 and inferred_tier in _TIER_TO_GROUP:
        return _TIER_TO_GROUP[inferred_tier]
    # T1/T2/T3/T4: 等 dashboard 确认 tier log 后再路由.
    return None


async def _assess_supply(
    session, item: ModelIntelItem,
) -> tuple[Optional[Resolution], bool, Optional[str]]:
    """解析 canonical + 判供给（O1 闸门）

    Returns:
        (resolution, suppliable, supply_reason)
        - 闸门关闭 / 解析异常 → (None, True, None): 降级为旧行为 (照常写 pending),
          解析层是增强项, 不能因为它挂了就吞掉情报。
    """
    if not SUPPLY_GATE_ENABLED:
        return None, True, None
    try:
        resolution = await resolve_canonical_model(
            session, item.model_id, item.display_name,
        )
        suppliable, reason = await is_suppliable_by_any_channel(
            session,
            raw_model_id=item.model_id,
            canonical=resolution.canonical,
            source_provider=item.source_provider,
        )
        return resolution, suppliable, reason
    except Exception as e:
        logger.warning(
            "模型名解析/供给判定失败, 降级为旧行为: model=%s err=%s",
            item.model_id, e,
        )
        return None, True, None


async def _log_resolution_conflicts(
    session, canonical: str, item: ModelIntelItem,
) -> list[str]:
    """解析命中现网模型后, 只把"真冲突"写进应用日志 (不落待确认队列)

    2026-11-04 决策: 队列只放"要人做决定"的东西。既然已经确定是现网模型,
    intel 说的 modality / context_length 与 registry 不一致只是**参考信息**,
    落一条 approved=0 的日志只会重新制造噪声 (而且 approve 对这些字段本来
    也不生效, 见 crud.approve_capability_change)。
    """
    registry = await crud.get_model_registry(session, canonical)
    if registry is None:
        return []
    diffs: list[str] = []
    if item.modality and registry.modality and item.modality != registry.modality:
        diffs.append(f"modality {registry.modality}→{item.modality}")
    if (
        item.context_length
        and registry.context_length
        and item.context_length != registry.context_length
    ):
        diffs.append(f"context_length {registry.context_length}→{item.context_length}")
    if diffs:
        logger.info(
            "intel 与现网模型字段不一致 (仅留痕, 不改 registry): model=%s 现网=%s 差异=%s",
            item.model_id, canonical, ", ".join(diffs),
        )
    return diffs


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
        # 护栏 (2026-10-12): 渠道仍有价行供应的 deprecated 模型不自动出组.
        # registry 不标 deprecated (仍进路由), 组员不软删; 降级为
        # remove_from_group suggestion + pending capability log 待人工确认.
        # v4-flash 教训: 9/28 sensenova/taotoken 仍 0 元供应却被一步踢出 strong/code.
        if await crud.has_available_channel(session, model_id):
            await crud.log_capability_change(
                session,
                model_id=model_id,
                field_changed="status",
                old_value="active",
                new_value="deprecated (intel 判定, 待人工确认: 渠道仍供应)",
                source="ai_infer",
                confidence=item.confidence,
                approved=0,
                provider=item.source_provider,
            )
            active_groups = await _guardrail_active_groups(session, model_id)
            await _ensure_remove_suggestion(session, model_id, active_groups, item)
            logger.info(
                "intel_auto 护栏: model=%s deprecated+conf=%.2f 但渠道仍供应, "
                "降级为 suggestion 待人工确认 (不自动出组)",
                model_id, item.confidence,
            )
            return SyncResult(
                model_id=model_id, action="pending_review",
                confidence=item.confidence,
                reason="deprecated+conf>=0.9 但渠道仍有价行供应, 待人工确认",
            )

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
                provider=item.source_provider,
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
        # O1 (2026-11-04): 先解析规范名 + 判供给, 再决定要不要落待确认队列
        resolution, suppliable, supply_reason = await _assess_supply(session, item)

        # 2a) 本站没有任何供应证据 → 只留观察日志, 不进队列
        #     (volcengine 文档页抓来的 Seedream / Seedance 这类"只有名字"的条目)
        if not suppliable:
            logger.info(
                "待确认跳过 (本站无供应证据): model=%s provider=%s conf=%.2f "
                "— 不写 model_capabilities_log",
                model_id, item.source_provider, item.confidence,
            )
            return SyncResult(
                model_id=model_id, action="unsupplied",
                confidence=item.confidence,
                reason=f"无供应证据 (provider={item.source_provider or 'unknown'})",
            )

        # 2b) 确定性解析命中现网模型 → 不是"新模型", 不入队列
        if resolution is not None and resolution.resolved and not resolution.is_proposal:
            await _log_resolution_conflicts(session, resolution.canonical, item)
            logger.debug(
                "待确认跳过 (已解析到现网模型): model=%s ≈ %s (%s)",
                model_id, resolution.canonical, resolution.method,
            )
            return SyncResult(
                model_id=model_id, action="resolved",
                confidence=item.confidence,
                target_group=resolution.canonical,
                reason=f"已解析到 {resolution.canonical} ({resolution.method})",
            )

        # 2c) 其余照旧写 log; 启发式解析的结果一并挂上 (批准后即成为别名记忆)
        payload = item.model_dump()
        if resolution is not None and resolution.resolved:
            payload.update(resolution.as_payload())
            payload["resolution_note"] = (
                "启发式解析, 请确认; 批准后本次解析会记为别名, 后续扫描自动命中"
            )
        if supply_reason:
            payload["supply_evidence"] = supply_reason
        await crud.log_capability_change(
            session,
            model_id=model_id,
            field_changed="metadata",
            old_value=None,
            new_value=json.dumps(payload, ensure_ascii=False),
            source="ai_infer",
            confidence=item.confidence,
            approved=0,
            provider=item.source_provider,
        )
        action = "pending_review" if item.confidence >= 0.6 else "rejected"
        logger.debug(
            "待确认: model=%s conf=%.2f tier=%s family=%s resolved=%s (action=%s)",
            model_id, item.confidence, item.inferred_tier, item.family,
            resolution.canonical if resolution else None, action,
        )
        return SyncResult(
            model_id=model_id, action=action,
            confidence=item.confidence, reason="conf<tier 不达标",
        )

    # 3) 高置信度入组
    target_group = await _find_target_group(session, item.inferred_tier)
    existing = await crud.get_model_registry(session, model_id)
    if not existing:
        # V4.8.6: 入库前校验渠道可用性 — 无可用渠道时 status=inactive,
        # 路由 cache 只加载 status=active 的模型, inactive 模型天然不会被选中.
        # scheduler 周期复扫会在用户配好渠道后自动翻 active.
        channel_ok = await crud.has_available_channel(session, model_id)
        new_value_meta = item.model_dump()
        if channel_ok:
            initial_status = item.status or "active"
        else:
            initial_status = "inactive"
            new_value_meta["channel_available"] = False
            logger.warning(
                "新模型入库但无可用渠道: model=%s (provider=%s) — 以 status=inactive 入库, "
                "待用户配好渠道后由 scheduler 复扫自动翻 active",
                model_id, item.source_provider,
            )
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
            status=initial_status,
            notes=f"auto-joined from intel (conf={item.confidence:.2f})",
        )
        await crud.log_capability_change(
            session,
            model_id=model_id,
            field_changed="create",
            old_value=None,
            new_value=json.dumps(new_value_meta, ensure_ascii=False),
            source="ai_infer",
            confidence=item.confidence,
            approved=0,
            provider=item.source_provider,
        )
    else:
        # V4.8.6: 渠道恢复 — 之前因无渠道以 inactive 入库的模型,
        # 用户配好渠道后下次扫描在这里自动翻 active.
        if existing.status == "inactive":
            channel_ok = await crud.has_available_channel(session, model_id)
            if channel_ok:
                existing.status = item.status or "active"
                logger.info(
                    "渠道恢复: model=%s inactive -> active (用户已配置可用渠道)",
                    model_id,
                )

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
                provider=item.source_provider,
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
                provider=item.source_provider,
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
                provider=item.source_provider,
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
            provider=item.source_provider,
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

    # V4.8.6: 从 raw_data 顶层取 provider, 注入到每个 model item (若 item 自身未带)
    top_provider = data.get("provider")

    results: list[SyncResult] = []
    async with async_session_factory() as session:
        for raw in models:
            # 1) Pydantic 验证是 sync 阶段, 失败 skip 后续不写表
            if top_provider and not raw.get("source_provider"):
                raw = {**raw, "source_provider": top_provider}
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
