"""数据访问操作层

提供各表的增删改查操作。
"""

from datetime import date
import logging
from typing import Optional

from sqlalchemy import select, update
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
    """将 model 加入组 (Phase 3.3 自动入组)，已存在则跳过。

    Returns:
        True 新加入, False 已存在
    """
    from db.models import ModelGroupMember
    from sqlalchemy import select

    stmt = select(ModelGroupMember).where(
        ModelGroupMember.group_id == group_id,
        ModelGroupMember.model_id == model_id,
    )
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing:
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
    - 返回更新后的 log 行 (供 dashboard 二次确认)
    """
    from db.models import ModelCapabilitiesLog, ModelRegistry
    from sqlalchemy import update

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
            registry.tier = log.new_value
            logger.info("人工批准: model=%s tier %s -> %s",
                       log.model_id, registry.tier, log.new_value)
    elif log.field_changed == "create":
        # 首次创建: 不动 registry (已由 model_sync 创建, 此处仅确认 metadata)
        pass
    elif log.field_changed == "status" and log.new_value == "deprecated":
        # 出组已被 L2 高置信度自动批准 (approved=1), 此路径通常不命中
        registry = await session.get(ModelRegistry, log.model_id)
        if registry:
            registry.status = "deprecated"
    # capabilities / context_length / modality 变更为参考项, 不直接覆盖 registry

    await session.flush()
    return log


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
