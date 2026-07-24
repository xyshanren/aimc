"""数据访问操作层

提供各表的增删改查操作。
"""

import logging
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import (
    Channel, Account, PriceSnapshot, Promotion,
    ChannelQuota, UsageLog, ParseRule, IntelSource,
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
