"""默认模型组/注册表种子 (2026-10-14, 初始化配置配套)

目的: 全新部署 (看板配完渠道后) 自动获得可用的 tier 组路由,
不再依赖手动跑 seed_model_registry.py / seed_model_groups.py。

设计约束 (与 scripts/seed_model_groups.py 生产形态对齐):
- **ensure 语义**: 只补缺失 (INSERT-if-missing), 绝不改已有行 —
  生产已策展的组/成员/registry 不受任何影响, 重跑零 diff;
  软删 (is_active=0) 的组成员行视为策展移出, 跳过不复活也不重建
  (UNIQUE 槽位占用, 直接 INSERT 会撞约束 — 2026-10-15 修)
- **成员只按渠道挂**: 只为本渠道价格行对应的模型建组成员 (ensure_group_members
  接收单个渠道 cfg), 不碰其他渠道的成员资格
- tier→组映射与各组过滤器自洽:
    T0→flagship(不限)  T1→strong(不限)  T2→balanced(过滤T2✓)  T3/T4→light(不限,
    2026-10-15 对齐生产: 原 T3 过滤会把 T4 成员全跳过致 503)
    multimodal/vision → vision(T0/T1) 或 vision-light(T2+)
  精细控制 (catalog 字段, 2026-10-15 与生产策展对齐):
    specialized="tier:xxx" — 特化模型只进该组 (tier:code 只放特化代码模型)
    auto_group=False — registry 保留行但不入组 (生产策展移出的模型)
    group_priorities={组:优先级} — 入组用生产精确值, 缺失回落 max+5 递增
  prod 教训 "精确 tier 过滤只添乱" 的组 (flagship/strong/vision/code) 均为
  NULL tier, 自动映射成员不会被过滤器误杀
- 生图/观察期模型不入组: catalog prices 有行但 MODEL_REGISTRY 无条目的模型
  (u1.5 系 / qwen3-next 等) 不建 registry 行 (避免幽灵路由候选)

用法 (dashboard/onboarding.py 调用):
    await seed_default.ensure_default_groups(session)
    await seed_default.ensure_registry_and_members(session, cfg)
"""

import json
import logging

from sqlalchemy import select

from channels_catalog import MODEL_REGISTRY, RAW_ALIASES
from db.models import ModelGroup, ModelGroupMember, ModelRegistry
from gateway.group_rules import group_runtime_accepts

logger = logging.getLogger(__name__)


class _MetaRegView:
    """catalog 元信息 → 组准入判定用的 registry 视图 (新模型尚未落库时用)"""

    __slots__ = ("model_id", "tier", "modality", "capabilities", "context_length")

    def __init__(self, meta: dict, model_id: str = ""):
        self.model_id = model_id
        self.tier = meta.get("tier")
        self.modality = meta.get("modality", "text")
        self.capabilities = json.dumps(meta.get("capabilities") or [])
        self.context_length = meta.get("context_length")

# 组定义 (照搬 scripts/seed_model_groups.py V6.14 形态, 7 组 + fallback 链)
# 字段: (group_id, display_name, tier, capabilities, min_context,
#        require_vision, strategy, fallback_group, sort_order)
DEFAULT_GROUPS = [
    ("tier:flagship", "旗舰推理组", None, None, 64000, 0, "cheapest", "tier:strong", 10),
    ("tier:strong", "主力通用组", None, None, 0, 0, "cheapest", "tier:balanced", 20),
    ("tier:balanced", "均衡性价比组", "T2", None, 0, 0, "cheapest", "tier:light", 30),
    ("tier:light", "轻量免费组", None, None, 0, 0, "cheapest", None, 40),
    ("tier:vision", "多模态视觉组", None, None, 0, 1, "cheapest", "tier:vision-light", 25),
    ("tier:vision-light", "轻量视觉组", None, None, 0, 1, "cheapest", "tier:light", 26),
    # 代码专用组 (V6.17d): 要求 code_specialized 能力 —— 运行时强制"代码特化",
    # 泛用模型 (capabilities 含 code 但非特化) 加不进来; 看板候选列表同源镜像
    ("tier:code", "代码专用组", None, '["code_specialized"]', 0, 0, "cheapest", "tier:balanced", 50),
]

TIER_RANK = {"T0": 0, "T1": 1, "T2": 2, "T3": 3, "T4": 4}
SEED_ADDED_BY = "catalog-seed"


def _tier_groups(meta: dict) -> list[str]:
    """模型 tier/能力 → 目标组列表 (映射规则见模块 docstring)"""
    # deprecated 模型不入组 (2026-10-15 对齐生产: deepseek-v4-flash 等被替代模型
    # registry 保留行但组内无成员, 直接路由仍走渠道价行)
    if meta.get("status") == "deprecated":
        return []
    # 特化模型只进特化组 (2026-10-15): kimi-k2.7-code/qwen3-coder 只进 tier:code,
    # glm-4.6v-flash 只进 tier:vision-light — 生产 tier:code 只放特化代码模型,
    # 泛用 capabilities 含 'code' 不再作为入组信号
    if meta.get("specialized"):
        return [meta["specialized"]]
    # 生产策展移出组的模型 (registry 保留行仅直接路由, 如 deepseek-v3/qwen2.5-72b)
    if not meta.get("auto_group", True):
        return []
    tier = meta.get("tier")
    modality = meta.get("modality", "text")
    groups: list[str] = []
    if tier in ("T0",):
        groups.append("tier:flagship")
    elif tier in ("T1",):
        groups.append("tier:strong")
    elif tier in ("T2",):
        groups.append("tier:balanced")
    elif tier in ("T3", "T4"):
        groups.append("tier:light")
    if modality in ("vision", "multimodal"):
        groups.append("tier:vision" if tier in ("T0", "T1") else "tier:vision-light")
    return groups


async def ensure_default_groups(session) -> int:
    """确保 7 个默认组存在 (只补缺失, 不改已有); 返回新建数"""
    existing = set((await session.execute(
        select(ModelGroup.group_id)
    )).scalars().all())
    created = 0
    for gid, dname, tier, caps, min_ctx, req_vision, strategy, fb, order in DEFAULT_GROUPS:
        if gid in existing:
            continue
        session.add(ModelGroup(
            group_id=gid, display_name=dname, tier=tier,
            capabilities=caps, min_context=min_ctx,
            require_vision=req_vision, strategy=strategy,
            fallback_group=fb, sort_order=order,
        ))
        created += 1
    if created:
        await session.flush()
        logger.info(f"[seed-default] 新建默认组 {created} 个: "
                    f"{sorted(set(g[0] for g in DEFAULT_GROUPS) - existing)}")
    return created


async def ensure_registry_and_members(session, cfg: dict) -> dict:
    """为单个渠道建 registry 行 + 按目录 tier 自动入组 (ensure 语义)

    只处理该渠道价格行对应的模型 (RAW_ALIASES 映射到规范名, 无 alias 的
    raw 行跳过 — 聚合平台观察期模型保持不入组); 返回统计 dict。
    """
    # 1) 价格行模型名 -> 规范名集合 (保序去重)
    canon_models: list[str] = []
    for row in cfg["prices"]:
        model = row[0]
        canon = RAW_ALIASES.get(model, model)
        if canon in MODEL_REGISTRY and canon not in canon_models:
            canon_models.append(canon)

    stats = {"registry_created": 0, "members_created": 0, "models": canon_models}
    if not canon_models:
        return stats

    # 2) ensure registry 行
    existing_models = set((await session.execute(
        select(ModelRegistry.model_id)
    )).scalars().all())
    for canon in canon_models:
        if canon in existing_models:
            continue
        meta = MODEL_REGISTRY[canon]
        session.add(ModelRegistry(
            model_id=canon,
            family=meta["family"],
            display_name=canon.replace("-", " ").title(),
            tier=meta["tier"],
            capabilities=json.dumps(meta.get("capabilities") or []),
            context_length=meta.get("context_length"),
            modality=meta.get("modality", "text"),
            status=meta.get("status", "active"),
        ))
        stats["registry_created"] += 1
    if stats["registry_created"]:
        await session.flush()

    # 3) ensure 默认组 (成员入组前组必须存在)
    await ensure_default_groups(session)

    # 3b) 组定义 + registry 快照 (入组前校验组约束, V6.17d)
    #     种子不得造出"加进组但路由会跳过"的死成员 (如旗舰组 ctx>=64000、
    #     代码组要求 code_specialized) —— 校验规则与路由层同源
    group_rows = {g.group_id: g for g in (await session.execute(
        select(ModelGroup))).scalars().all()}
    reg_rows = {r.model_id: r for r in (await session.execute(
        select(ModelRegistry))).scalars().all()}

    # 4) ensure 组成员 (本渠道模型 only; priority 按组内已有最大值递增)
    valid_groups = set((await session.execute(
        select(ModelGroup.group_id)
    )).scalars().all())
    existing_members = set((await session.execute(
        select(ModelGroupMember.group_id, ModelGroupMember.model_id)
        .where(ModelGroupMember.is_active == 1)
    )).all())
    existing_members = {(g, m) for g, m in existing_members}

    for canon in canon_models:
        meta = MODEL_REGISTRY[canon]
        for gid in _tier_groups(meta):
            # specialized 等自定义组目标必须真实存在, 否则 INSERT 出
            # 路由永远看不见的孤儿成员行 (2026-10-16 防御)
            if gid not in valid_groups:
                logger.warning(
                    f"[seed-default] {canon} 目标组 {gid!r} 不存在, 跳过入组")
                continue
            # 入组前校验组约束 (V6.17d): 不满足组定义 (tier/caps/min_ctx/require_vision)
            # 的模型不入组 —— 否则造出路由永远跳过的死成员
            grp = group_rows.get(gid)
            if grp is not None:
                reg_view = reg_rows.get(canon) or _MetaRegView(meta)
                _ok, _reason = group_runtime_accepts(grp, reg_view)
                if not _ok:
                    logger.warning(
                        f"[seed-default] {canon} 不满足组 {gid!r} 约束 ({_reason}), 跳过入组")
                    continue
            if (gid, canon) in existing_members:
                continue
            # 组内 priority: catalog group_priorities 给的 (生产精确值) 优先,
            # 缺失时按组内已有最大值 +5 递增 (保序, 不与人工排位冲突)
            hints = meta.get("group_priorities") or {}
            pri = hints.get(gid)
            if pri is None:
                max_pri = (await session.execute(
                    select(ModelGroupMember.priority)
                    .where(ModelGroupMember.group_id == gid,
                           ModelGroupMember.is_active == 1)
                )).scalars().all()
                pri = (max(max_pri) + 5) if max_pri else 100
            # 软删行占着 UNIQUE(group_id, model_id) 槽位, 直接 INSERT 会撞约束 500
            # (2026-10-15 修)。但这里**跳过而非复活**: 软删 = 策展有意移出, 自动补种
            # 不得翻案 (复活只走看板手动加成员的显式意图)
            stale = (await session.execute(
                select(ModelGroupMember.id).where(
                    ModelGroupMember.group_id == gid,
                    ModelGroupMember.model_id == canon)
            )).scalars().first()
            if stale is not None:
                logger.debug(
                    f"[seed-default] {gid}/{canon} 有软删行 (策展移出), 跳过不复活")
                continue
            session.add(ModelGroupMember(
                group_id=gid, model_id=canon,
                priority=pri, weight=1.0, max_cost_ratio=None,
                added_by=SEED_ADDED_BY,
            ))
            existing_members.add((gid, canon))
            stats["members_created"] += 1

    if stats["members_created"]:
        await session.flush()
        logger.info(
            f"[seed-default] 渠道 {cfg['name']}: registry +{stats['registry_created']}, "
            f"组成员 +{stats['members_created']} ({canon_models})")
    return stats


async def seed_all_catalog_channels(session) -> dict:
    """全目录补种 (看板「初始化默认模型组」按钮): 只处理已创建的渠道

    幂等: 重跑零 diff (只补缺失)。仅 DB 里已有 channel 行的目录渠道参与 —
    未建渠道的目录模型不建 registry/组成员 (避免幽灵路由候选:
    组有成员但无渠道供给)。
    """
    from channels_catalog import CHANNELS
    from db.models import Channel

    await ensure_default_groups(session)
    existing = set((await session.execute(
        select(Channel.name)
    )).scalars().all())
    total = {"registry_created": 0, "members_created": 0, "channels": {}}
    for cfg in CHANNELS:
        if cfg.get("onboarding", {}).get("hidden"):
            continue
        if cfg["name"] not in existing:
            continue  # 渠道未创建 — 不建幽灵候选
        stats = await ensure_registry_and_members(session, cfg)
        if stats["registry_created"] or stats["members_created"]:
            total["channels"][cfg["name"]] = stats
        total["registry_created"] += stats["registry_created"]
        total["members_created"] += stats["members_created"]
    return total
