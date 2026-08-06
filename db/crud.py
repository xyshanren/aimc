"""数据访问操作层

提供各表的增删改查操作。
"""

from datetime import date
import logging
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import (
    Channel, Account, PriceSnapshot, Promotion,
    ChannelQuota, UsageLog, ParseRule, IntelSource,
    ModelCard, SettingsRuntime,
)

logger = logging.getLogger(__name__)


# === 渠道操作 ===

async def create_channel(session: AsyncSession, **kwargs) -> Channel:
    channel = Channel(**kwargs)
    session.add(channel)
    await session.flush()
    return channel


async def get_channel(session: AsyncSession, channel_id: int) -> Optional[Channel]:
    return await session.get(Channel, channel_id)


async def list_channels(session: AsyncSession, status: str = "active") -> list[Channel]:
    stmt = select(Channel).where(Channel.status == status)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_channel_by_provider(
    session: AsyncSession,
    provider: str,
    status: str = "active",
) -> Optional[Channel]:
    """按 provider 查首个 active channel。

    Phase 3.4 N+1 优化 (I8): 替代 "list_channels 全表 + next find" 模式，
    单条 SELECT 命中索引，避免在大规模 channel 下做无意义扫描。

    多 channel 同 provider 场景 (e.g. 多个 regional endpoint 或多账号独立 channel)
    时返回 id 最小的 (最先注册的那个)，与原 list_channels + next 实现保持一致。
    返回 None 表示该 provider 没有 active channel。
    """
    stmt = (
        select(Channel)
        .where(Channel.provider == provider)
        .where(Channel.status == status)
        .order_by(Channel.id)
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def list_active_channels_by_provider(
    session: AsyncSession,
    provider: str,
) -> list[Channel]:
    """按 provider 查所有 active channels。

    Phase 3.6 (2026-08-06): 供 scheduler._resolve_api_key 使用,
    拿该 provider 的所有 active channel 的 api_key (一般个人用只有 1 个,
    多 channel 场景返回 id 升序, 取第一个 key 鉴权)。
    """
    stmt = (
        select(Channel)
        .where(Channel.provider == provider)
        .where(Channel.status == "active")
        .order_by(Channel.id)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def update_channel_status(session: AsyncSession, channel_id: int, status: str):
    stmt = update(Channel).where(Channel.id == channel_id).values(status=status)
    await session.execute(stmt)


# === 账号操作 ===

async def create_account(session: AsyncSession, **kwargs) -> Account:
    account = Account(**kwargs)
    session.add(account)
    await session.flush()
    return account


async def get_accounts_by_channel(session: AsyncSession, channel_id: int) -> list[Account]:
    stmt = select(Account).where(Account.channel_id == channel_id)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def update_account_quota(session: AsyncSession, account_id: int, used_delta: int):
    """增量更新账号已用配额"""
    account = await session.get(Account, account_id)
    if account:
        account.quota_used = (account.quota_used or 0) + used_delta
        # 检查是否耗尽
        if account.quota_total and account.quota_used >= account.quota_total:
            account.status = "exhausted"
            logger.info(f"账号 {account.account_name} 配额已耗尽")


# === 价格快照操作 ===

async def create_price_snapshot(session: AsyncSession, **kwargs) -> PriceSnapshot:
    snapshot = PriceSnapshot(**kwargs)
    session.add(snapshot)
    await session.flush()
    return snapshot


async def get_prices_by_channel(session: AsyncSession, channel_id: int) -> list[PriceSnapshot]:
    stmt = select(PriceSnapshot).where(PriceSnapshot.channel_id == channel_id)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_prices_by_model(session: AsyncSession, model: str) -> list[PriceSnapshot]:
    stmt = select(PriceSnapshot).where(PriceSnapshot.model == model)
    result = await session.execute(stmt)
    return list(result.scalars().all())


# Phase 3.4 (2026-07-29): 价格变动更新/upsert + 路由缓存刷新
async def get_active_price_snapshots(
    session: AsyncSession,
    model: str,
    channel_id: Optional[int] = None,
) -> list[PriceSnapshot]:
    """获取 model 有效期内价格快照, 供 3.4 对比基准"""
    stmt = select(PriceSnapshot).where(PriceSnapshot.model == model)
    if channel_id is not None:
        stmt = stmt.where(PriceSnapshot.channel_id == channel_id)
    result = await session.execute(stmt)
    today = date.today().isoformat()
    return [p for p in result.scalars().all() if _is_price_in_range(p, today)]


def _is_price_in_range(price: PriceSnapshot, today: str) -> bool:
    if price.effective_from and price.effective_from > today:
        return False
    if price.effective_to and price.effective_to < today:
        return False
    return True


async def upsert_price_snapshot(
    session: AsyncSession,
    channel_id: int,
    model: str,
    input_price: Optional[float],
    output_price: Optional[float],
    source: str = "crawler",
    price_type: str = "normal",
    time_range: str = "全天",
    effective_from: Optional[str] = None,
    effective_to: Optional[str] = None,
) -> tuple[PriceSnapshot, bool]:
    """按 (channel, model, price_type, time_range) upsert 价椌快照

    Returns:
        (PriceSnapshot, created) created=True 表示新建, False 表示更新

    Phase 3.4: 价椌变动会调用此函数, 以 source=crawler 标记便于区分手动录入.
    """
    stmt = select(PriceSnapshot).where(
        PriceSnapshot.channel_id == channel_id,
        PriceSnapshot.model == model,
        PriceSnapshot.price_type == price_type,
        PriceSnapshot.time_range == time_range,
    )
    result = await session.execute(stmt)
    snapshot = result.scalar_one_or_none()

    if snapshot:
        snapshot.input_price = input_price
        snapshot.output_price = output_price
        snapshot.source = source
        snapshot.effective_from = effective_from or snapshot.effective_from
        snapshot.effective_to = effective_to or snapshot.effective_to
        return snapshot, False

    snapshot = PriceSnapshot(
        channel_id=channel_id,
        model=model,
        price_type=price_type,
        time_range=time_range,
        input_price=input_price,
        output_price=output_price,
        unit="元/百万tokens",
        effective_from=effective_from,
        effective_to=effective_to,
        source=source,
    )
    session.add(snapshot)
    await session.flush()
    return snapshot, True


# === 优惠活动操作 ===

async def create_promotion(session: AsyncSession, **kwargs) -> Promotion:
    promo = Promotion(**kwargs)
    session.add(promo)
    await session.flush()
    return promo


async def list_promotions(session: AsyncSession, status: str = "active", limit: int = 50) -> list[Promotion]:
    stmt = (
        select(Promotion)
        .where(Promotion.status == status)
        .order_by(Promotion.created_at.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def expire_promotion(session: AsyncSession, promo_id: int):
    stmt = update(Promotion).where(Promotion.id == promo_id).values(status="expired")
    await session.execute(stmt)


# === 频限配额操作 ===

async def create_quota(session: AsyncSession, **kwargs) -> ChannelQuota:
    quota = ChannelQuota(**kwargs)
    session.add(quota)
    await session.flush()
    return quota


async def get_quotas_by_channel(session: AsyncSession, channel_id: int) -> list[ChannelQuota]:
    stmt = select(ChannelQuota).where(ChannelQuota.channel_id == channel_id)
    result = await session.execute(stmt)
    return list(result.scalars().all())


# === 用量日志操作 ===

async def create_usage_log(session: AsyncSession, **kwargs) -> UsageLog:
    log = UsageLog(**kwargs)
    session.add(log)
    await session.flush()
    return log


# === 解析规则操作 ===

async def get_parse_rules(session: AsyncSession, provider: str) -> list[ParseRule]:
    stmt = select(ParseRule).where(
        ParseRule.provider == provider,
        ParseRule.status == "active",
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_active_parse_rule(session: AsyncSession, provider: str) -> Optional[ParseRule]:
    """获取 provider 当前 active 的最高 confidence 解析规则

    Phase 2B-2: AI 解析失败时降级用
    """
    stmt = (
        select(ParseRule)
        .where(
            ParseRule.provider == provider,
            ParseRule.status == "active",
        )
        .order_by(ParseRule.confidence.desc())
    )
    result = await session.execute(stmt)
    return result.scalars().first()


async def upsert_parse_rule(session: AsyncSession, provider: str, **kwargs) -> ParseRule:
    """创建或更新解析规则"""
    stmt = select(ParseRule).where(ParseRule.provider == provider)
    result = await session.execute(stmt)
    rule = result.scalar_one_or_none()

    if rule:
        for key, value in kwargs.items():
            setattr(rule, key, value)
    else:
        rule = ParseRule(provider=provider, **kwargs)
        session.add(rule)

    await session.flush()
    return rule


# === 情报采集源操作 ===

async def get_intel_sources(session: AsyncSession, status: str = "active") -> list[IntelSource]:
    stmt = select(IntelSource).where(IntelSource.status == status)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def update_intel_source(session: AsyncSession, source_id: int, **kwargs):
    stmt = update(IntelSource).where(IntelSource.id == source_id).values(**kwargs)
    await session.execute(stmt)


async def get_intel_source_by_provider_url(
    session: AsyncSession, provider: str, url: str
) -> Optional[IntelSource]:
    """按 (provider, url) 查 IntelSource 行

    Phase 2C-2: 替代 source_id 关联（dataclass.source_id 默认为 0，无法用）
    语义：每个 source 的 (provider, url) 是唯一标识，跟 DB IntelSource 表的 url 列对应
    """
    stmt = select(IntelSource).where(
        IntelSource.provider == provider,
        IntelSource.url == url,
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def upsert_intel_source(
    session: AsyncSession,
    provider: str,
    url: str,
    **kwargs,
) -> IntelSource:
    """按 (provider, url) 创建或更新 IntelSource

    用于 scheduler 启动时把 dataclass 跟 DB 行关联起来
    """
    existing = await get_intel_source_by_provider_url(session, provider, url)
    if existing:
        for key, value in kwargs.items():
            setattr(existing, key, value)
        return existing
    else:
        # source_type 已经在 kwargs 里（调用方传）→ 不要再写默认值
        # 修复：之前 hardcode source_type="web_page" + **kwargs 会冲突 (2026-07-24)
        new = IntelSource(
            provider=provider,
            url=url,
            status="active",
            poll_interval=900,
            **kwargs,
        )
        session.add(new)
        await session.flush()
        return new


# === ModelRegistry 操作 (Phase 3.3, 2026-07-29) ===

async def get_model_registry(session: AsyncSession, model_id: str) -> Optional["ModelRegistry"]:
    """按 model_id 查 ModelRegistry (Phase 3.3 消费者)"""
    from db.models import ModelRegistry
    return await session.get(ModelRegistry, model_id)


async def upsert_model_registry(
    session: AsyncSession,
    model_id: str,
    family: str,
    tier: str = "T2",
    **kwargs,
) -> "ModelRegistry":
    """按 model_id upsert ModelRegistry。

    Phase 3.3: 自动入组时调用。
    tier 字段永远走 human-approved 路径 (created/updated tier 仅当 changed=False 时写入)；
    AI 推断的 tier 只能写 capabilities_log，待 dashboard 确认后才生效。
    """
    from db.models import ModelRegistry

    existing = await session.get(ModelRegistry, model_id)
    if existing:
        for key, value in kwargs.items():
            setattr(existing, key, value)
        return existing
    row = ModelRegistry(model_id=model_id, family=family, tier=tier, **kwargs)
    session.add(row)
    await session.flush()
    return row


async def add_model_group_member(
    session: AsyncSession,
    group_id: str,
    model_id: str,
    priority: int = 100,
    weight: float = 1.0,
    added_by: str = "manual",
) -> bool:
    """将 model 加入组 (Phase 3.3 自动入组)，已存在 (不论是否 active) 则跳过。

    P1-2 fix: 返回 False 包含 2 种场景
    - 是 active (已入组)
    - 是 inactive (已被出组) -> 转为 active (覆盖之前的 removed_* 字段)
    上面 2 种都可以看作"成员已存在", 不重复入。

    Returns:
        True 新加入, False 已存在 (含 active 和 inactive 两种)
    """
    from db.models import ModelGroupMember
    from sqlalchemy import select

    stmt = select(ModelGroupMember).where(
        ModelGroupMember.group_id == group_id,
        ModelGroupMember.model_id == model_id,
    )
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing:
        # P1-2: 之前因 deprecated / tier 迁移被出组, 现重新入组 -> 打开 active
        if not existing.is_active:
            existing.is_active = 1
            existing.removed_at = None
            existing.removed_reason = None
            existing.removed_by = None
            await session.flush()
        return False
    member = ModelGroupMember(
        group_id=group_id,
        model_id=model_id,
        priority=priority,
        weight=weight,
        added_by=added_by,
    )
    session.add(member)
    await session.flush()
    return True


async def remove_model_group_members_by_model(
    session: AsyncSession,
    model_id: str,
    *,
    reason: str = "manual",
    removed_by: str = "manual_approve",
) -> int:
    """软删除 model_id 在所有组中的成员关系 (P1-2 fix, 原 P1-1 是硬 DELETE)。

    P1-2 改为软删除: 设 is_active=0 + 记录 removed_at/reason/removed_by, 保留行。
    业务查询 path (路由 cache) 需主动 .where(is_active=1) 过滤。

    Phase 4 v2 (P1-2) 进期设计:
    - 架构 §5.4 描述 "DELETE FROM model_group_members" 的语义改为 "软出组"
    - 审计/回滚更容易: 哪天改错了可以从 model_capabilities_log 查状态再 cover
    - 需定期清理脚本 (delete_removed_members_older_than(90d)) 防表膨胀,
      后续运维补 job

    Args:
        model_id: 被出组的 model_id
        reason: 出组原因 (e.g. "deprecated+conf>=0.9", "tier_change_T2->T3")
        removed_by: 执行人 (intel_auto / manual_approve / heater)

    Returns:
        被软删的行数 (含原本就 active 但被设为 inactive)
    """
    from db.models import ModelGroupMember
    from sqlalchemy import update
    from datetime import datetime

    stmt = (
        update(ModelGroupMember)
        .where(
            ModelGroupMember.model_id == model_id,
            ModelGroupMember.is_active == 1,
        )
        .values(
            is_active=0,
            removed_at=datetime.now(),
            removed_reason=reason,
            removed_by=removed_by,
        )
    )
    result = await session.execute(stmt)
    return result.rowcount or 0


async def list_active_group_members_for_group(
    session: AsyncSession,
    group_id: str,
) -> list:
    """Phase 4 v2 (P1-2): 拉取某 group 下所有 active 成员。

    路由 cache 加载专用, 业务路径需主动过滤 is_active=1.
    """
    from db.models import ModelGroupMember
    from sqlalchemy import select

    stmt = (
        select(ModelGroupMember)
        .where(
            ModelGroupMember.group_id == group_id,
            ModelGroupMember.is_active == 1,
        )
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def find_target_group_for_tier(
    session: AsyncSession,
    tier: str,
) -> Optional[str]:
    """按 tier 找候选 group_id (P1-1 fix, 供 E1 approve 迁移用)。

    策略与 model_sync._find_target_group 保持一致:
    - T0 (旗舰) 硬编码路由到 "tier:flagship" (group.tier=NULL, 优先匹配旗舰)
    - 其他 tier 找 group.tier == tier 的组
    - 找不到返 None, caller 跳过 join

    为什么 model_sync 跟 crud 各一份: model_sync 走 ai_infer 路径不能
    改 model_group_members, 这里走人工 approve 路径可以走. 逻辑同源
    (不动 flagship 路由的硬编码) 避免总是从 model_sync 透出.
    """
    if tier == "T0":
        # flagship 组的 group_id 是定义里的硬编码, 不查表准上层可追踪
        return "tier:flagship"
    from db.models import ModelGroup
    from sqlalchemy import select

    stmt = (
        select(ModelGroup.group_id)
        .where(ModelGroup.group_id.startswith("tier:"))
        .where(ModelGroup.tier == tier)
        .where(ModelGroup.enabled == 1)
        .limit(1)
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    return row


async def log_capability_change(
    session: AsyncSession,
    model_id: str,
    field_changed: str,
    old_value: Optional[str],
    new_value: Optional[str],
    source: str = "ai_infer",
    confidence: Optional[float] = None,
    approved: int = 0,
) -> None:
    """写 model_capabilities_log (Phase 3.3 审计)。

    approved 默认 0，dashboard 人工确认后才置 1。
    """
    from db.models import ModelCapabilitiesLog

    row = ModelCapabilitiesLog(
        model_id=model_id,
        field_changed=field_changed,
        old_value=old_value,
        new_value=new_value,
        source=source,
        confidence=confidence,
        approved=approved,
    )
    session.add(row)
    await session.flush()


async def find_candidate_group_for_tier(tier: str) -> Optional[str]:
    """Phase 3.3: 按 tier 找一个候选 group_id。

    策略: 找 group_id 以 'tier:' 开头 且 group.tier == target_tier 的组。
    没有匹配返 None (caller 跳过自动入组)。
    """
    from db.models import ModelGroup
    from sqlalchemy import select

    # 暂时用同步路径, 供 Phase 3.3 消费: 加载 group 后比对
    return None  # 实现需在 model_sync 中用 session 调, 这里仅声明


# === Phase 4 E1: Pending Review (人工确认队列) ===

async def list_pending_review(
    session: AsyncSession,
    limit: int = 100,
) -> list["ModelCapabilitiesLog"]:
    """列出 approved=0 的能力变更 (Phase 4 待确认队列)。

    仅显示 ai_infer 来源的变更 (manual 直接 approved=1 不需确认)。
    按创建时间倒序 (最新在最上)。
    """
    from db.models import ModelCapabilitiesLog

    stmt = (
        select(ModelCapabilitiesLog)
        .where(ModelCapabilitiesLog.approved == 0)
        .where(ModelCapabilitiesLog.source == "ai_infer")
        .order_by(ModelCapabilitiesLog.created_at.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def approve_capability_change(
    session: AsyncSession,
    log_id: int,
) -> Optional["ModelCapabilitiesLog"]:
    """人工批准能力变更 (Phase 4 E1)。

    - 把 log 行 approved 置 1
    - 若 field_changed == "tier", 同步更新 model_registry.tier (唯一被允许的自动应用项)
    - 上层 dashboard route 会在 commit 后调 route_engine.refresh_cache() 让路由
      下次请求拿到新 tier + 新 group 成员表.
    """
    from db.models import ModelCapabilitiesLog, ModelRegistry

    log = await session.get(ModelCapabilitiesLog, log_id)
    if not log:
        return None
    if log.approved == 1:
        return log  # 已批准, 幂等

    log.approved = 1

    if log.field_changed == "tier" and log.new_value:
        # tier 是唯一被人工批准后可立即生效的字段
        # 创建或更新 ModelRegistry 的 tier (registry 可能不存在 -> 跳过)
        registry = await session.get(ModelRegistry, log.model_id)
        if registry:
            # P1-7 fix: 先捕获旧值, 再 mutate, 最后 logger,
            # 避免 logger 输出 "old=old" 错日志。
            old_tier = registry.tier
            registry.tier = log.new_value
            logger.info(
                "人工批准 tier 迁移: model=%s %s -> %s (log_id=%d)",
                log.model_id, old_tier, log.new_value, log_id,
            )
            # P1-1 fix: tier 迁移后, 要从旧 tier 组 move 到新 tier 组.
            # _find_target_group 同 model_sync._find_target_group 逻辑:
            # T0 → tier:flagship 硬编码, 其他 → 查 group.tier == new_value.
            removed = await remove_model_group_members_by_model(session, log.model_id)
            new_group = await find_target_group_for_tier(session, log.new_value)
            if new_group:
                joined = await add_model_group_member(
                    session, new_group, log.model_id,
                    priority=100, weight=1.0, added_by="manual_approve",
                )
                logger.info(
                    "tier 迁移后入组: model=%s -> group=%s (removed=%d, joined=%s)",
                    log.model_id, new_group, removed, joined,
                )
            else:
                logger.warning(
                    "tier 迁移后未找到目标组: model=%s tier=%s (路由不会命中)",
                    log.model_id, log.new_value,
                )
    elif log.field_changed == "create":
        # 首次创建: 不动 registry (已由 model_sync 创建, 此处仅确认 metadata)
        pass
    elif log.field_changed == "status" and log.new_value == "deprecated":
        # 出组已被 L2 高置信度自动批准 (approved=1), 此路径通常不命中
        registry = await session.get(ModelRegistry, log.model_id)
        if registry:
            old_status = registry.status
            registry.status = "deprecated"
            logger.info(
                "人工批准 status 迁移: model=%s %s -> deprecated (log_id=%d)",
                log.model_id, old_status, log_id,
            )
    # capabilities / context_length / modality 变更为参考项, 不直接覆盖 registry

    await session.flush()
    return log


# === Phase 4 E3: 跨源交叉验证 (Cross-Source Discrepancy) ===

async def list_cross_source_discrepancies(
    session: AsyncSession,
    status: str = "pending",
    limit: int = 100,
) -> list["CrossSourceDiscrepancy"]:
    """列出跨源分歧记录 (Phase 4 E3)。

    Args:
        status: pending / resolved / ignored (默认只显示待处理)
        limit: 最多返回条数
    """
    from db.models import CrossSourceDiscrepancy

    stmt = (
        select(CrossSourceDiscrepancy)
        .where(CrossSourceDiscrepancy.status == status)
        .order_by(CrossSourceDiscrepancy.discrepancy_score.desc(), CrossSourceDiscrepancy.created_at.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def upsert_cross_source_discrepancy(
    session: AsyncSession,
    model_id: str,
    field_changed: str,
    sources_json: str,
    discrepancy_score: float = 0.0,
) -> "CrossSourceDiscrepancy":
    """Upsert 跨源分歧。

    唯一键 (model_id, field_changed, status=pending)：pending 状态下同 model+field 只保留 1 条。
    若已有 pending 行：覆盖 sources_json + score + updated_at。
    若 resolved/ignored：不再覆盖（保留历史决策）。

    P0-7 fix: 并发扫描时两个 worker 都会走到 SELECT → None 分支,
    然后都尝试 INSERT, 后者会撞 UniqueConstraint(model_id, field_changed, status).
    修法: 捕获 IntegrityError 后回滚 + 重新 SELECT 已有 pending 行 + UPDATE,
    避免产生 “孤儿 pending 行” (重复扫描结果仍是 1 行而非 2 行).
    """
    from db.models import CrossSourceDiscrepancy

    existing = await session.execute(
        select(CrossSourceDiscrepancy)
        .where(CrossSourceDiscrepancy.model_id == model_id)
        .where(CrossSourceDiscrepancy.field_changed == field_changed)
        .where(CrossSourceDiscrepancy.status == "pending")
    )
    row = existing.scalar_one_or_none()

    if row:
        # P2-17 fix: 不再手动赋值 updated_at. CrossSourceDiscrepancy.updated_at 已有
        # onupdate=func.now() (db/models.py), flush 时 ORM 自动更新, 跟手工赋值等价
        # 且避免 SQLAlchemy session.identity map 跟 onupdate 双重写入 (后者覆盖前者反而慢).
        row.sources_json = sources_json
        row.discrepancy_score = discrepancy_score
        return row

    # INSERT path: 可能在并发下撞 UniqueConstraint.
    try:
        row = CrossSourceDiscrepancy(
            model_id=model_id,
            field_changed=field_changed,
            sources_json=sources_json,
            discrepancy_score=discrepancy_score,
            status="pending",
        )
        session.add(row)
        await session.flush()
        return row
    except IntegrityError:
        # 并发 winner 已先 INSERT, 回滚后重新 SELECT 同一行做 UPDATE.
        await session.rollback()
        existing2 = await session.execute(
            select(CrossSourceDiscrepancy)
            .where(CrossSourceDiscrepancy.model_id == model_id)
            .where(CrossSourceDiscrepancy.field_changed == field_changed)
            .where(CrossSourceDiscrepancy.status == "pending")
        )
        row2 = existing2.scalar_one_or_none()
        if row2 is None:
            # 极端情况: 并发 winner 立即 resolve, 唯一键冲突但 pending 已被覆盖.
            # 此时直接放弃本轮 upsert, 返回 None 让上层日志知晓.
            logger.warning(
                "upsert_cross_source_discrepancy 并发冲突但 pending 已消失: "
                "model=%s field=%s",
                model_id, field_changed,
            )
            return None
        row2.sources_json = sources_json
        row2.discrepancy_score = discrepancy_score
        # P2-17 fix: updated_at 走 ORM onupdate 自动更新 (同上注释)
        await session.flush()
        return row2


async def resolve_cross_source_discrepancy(
    session: AsyncSession,
    discrepancy_id: int,
    resolved_value: str,
    resolved_note: Optional[str] = None,
    ignored: bool = False,
) -> Optional["CrossSourceDiscrepancy"]:
    """人工处理跨源分歧 (Phase 4 E3)。

    Args:
        discrepancy_id: CrossSourceDiscrepancy.id
        resolved_value: 人工拍板的值 (JSON 字符串 for capabilities)
        resolved_note: 备注
        ignored: True 时 status='ignored' (区别于 'resolved')

    Returns:
        更新后的行 / None (不存在)
    """
    from datetime import datetime
    from db.models import CrossSourceDiscrepancy

    row = await session.get(CrossSourceDiscrepancy, discrepancy_id)
    if not row:
        return None
    if row.status != "pending":
        return row  # 已处理，幂等
    row.status = "ignored" if ignored else "resolved"
    row.resolved_value = resolved_value
    row.resolved_note = resolved_note
    row.resolved_at = datetime.now()
    await session.flush()
    return row


async def reject_capability_change(
    session: AsyncSession,
    log_id: int,
) -> Optional["ModelCapabilitiesLog"]:
    """人工拒绝能力变更 (Phase 4 E1)。

    - 把 log 行 approved 置 1 (拒绝 = 不再待确认)
    - 不动 model_registry (保留当前值)
    """
    from db.models import ModelCapabilitiesLog

    log = await session.get(ModelCapabilitiesLog, log_id)
    if not log:
        return None
    if log.approved == 1:
        return log  # 已处理
    log.approved = 1
    await session.flush()
    return log


# === ModelCard 操作 (Phase 2D-4, 2026-07-24) ===

async def upsert_model_card(session, model: str, benchmark: str, **kwargs) -> ModelCard:
    """插入或更新 ModelCard（用 model+benchmark 作为唯一键）"""
    from sqlalchemy import select

    stmt = select(ModelCard).where(
        ModelCard.model == model, ModelCard.benchmark == benchmark
    )
    existing = (await session.execute(stmt)).scalar_one_or_none()

    if existing:
        for k, v in kwargs.items():
            setattr(existing, k, v)
        return existing
    else:
        card = ModelCard(model=model, benchmark=benchmark, **kwargs)
        session.add(card)
        await session.flush()
        return card


async def list_model_cards(session, provider: str = None) -> list[ModelCard]:
    """列出 ModelCard，可按 provider 过滤"""
    from sqlalchemy import select

    stmt = select(ModelCard)
    if provider:
        stmt = stmt.where(ModelCard.provider == provider)
    stmt = stmt.order_by(ModelCard.model, ModelCard.benchmark)
    return list((await session.execute(stmt)).scalars().all())


async def get_best_model_for_tier(session, tier: str) -> ModelCard | None:
    """按 tier 找 score 最高的模型（用于路由决策）

    Returns:
        score > 0 的最高分 ModelCard；全为 0 时返回第一个
    """
    from sqlalchemy import select

    stmt = (
        select(ModelCard)
        .where(ModelCard.tier == tier)
        .order_by(ModelCard.score.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


# === SettingsRuntime 操作 (API-Sec-2, 2026-07-29) ===
# k-v 存储 runtime settings (alert_channel / bark_key / feishu_webhook_url)
# dashboard PUT 即时生效, alerter 优先读这里, fallback 到 .env

async def get_runtime_setting(session: AsyncSession, key: str) -> Optional[str]:
    """读 runtime override value. None = 没 override (用 .env)"""
    stmt = select(SettingsRuntime).where(SettingsRuntime.key == key)
    row = (await session.execute(stmt)).scalar_one_or_none()
    return row.value if row else None


async def set_runtime_setting(session: AsyncSession, key: str, value: Optional[str]) -> None:
    """写 runtime override. value=None 表示删除 override 回退 .env

    行为: 有则 update, 无则 insert (key 是主键)
    """
    from datetime import datetime
    from sqlalchemy import delete

    if value is None:
        # 删 override
        await session.execute(
            delete(SettingsRuntime).where(SettingsRuntime.key == key)
        )
    else:
        # 查存在否
        existing = (await session.execute(
            select(SettingsRuntime).where(SettingsRuntime.key == key)
        )).scalar_one_or_none()
        if existing:
            existing.value = value
            existing.updated_at = datetime.now()
        else:
            session.add(SettingsRuntime(key=key, value=value))
    await session.commit()


async def list_runtime_settings(session: AsyncSession) -> list[SettingsRuntime]:
    """列所有 runtime override (供 dashboard 查看)"""
    stmt = select(SettingsRuntime).order_by(SettingsRuntime.key)
    return list((await session.execute(stmt)).scalars().all())
