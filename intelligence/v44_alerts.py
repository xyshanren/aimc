"""V4.4 账号配额、活跃账号和成本异常告警。"""

import logging
from datetime import datetime, timedelta

from db.engine import async_session_factory
from db.models import Account, Channel, UsageLog
from intelligence.alerter import send_alert
from sqlalchemy import func, select

logger = logging.getLogger(__name__)


async def check_quota_alerts() -> int:
    """检查配额达到 80% 和 100% 的账号。"""
    sent = 0
    async with async_session_factory() as session:
        result = await session.execute(select(Account, Channel).join(Channel, Account.channel_id == Channel.id))
        for account, channel in result.all():
            if not account.quota_total or account.quota_total <= 0:
                continue
            ratio = (account.quota_used or 0) / account.quota_total
            if ratio >= 1:
                await send_alert(
                    f"{channel.provider} 账号 {account.account_name} 已耗尽，现有 active 账号将被检查",
                    key=f"account_exhausted:{account.id}",
                )
                sent += 1
            elif ratio > 0.8:
                await send_alert(
                    f"{channel.provider} 账号 {account.account_name} 额度已用 {ratio:.0%}，建议注册新账号",
                    key=f"quota_near_limit:{account.id}",
                )
                sent += 1
        await session.commit()
    return sent


async def check_no_active_accounts() -> int:
    """检查每个 provider 是否至少有一个可用 active 账号。"""
    sent = 0
    async with async_session_factory() as session:
        providers = await session.execute(select(Channel.provider).distinct())
        for provider in providers.scalars():
            stmt = select(func.count(Account.id)).join(Channel).where(
                Channel.provider == provider,
                Channel.status == "active",
                Account.status == "active",
            )
            active_count = (await session.execute(stmt)).scalar_one()
            if active_count == 0:
                await send_alert(
                    f"{provider} 已无 active 账号，路由将自动降级到其他渠道",
                    key=f"no_active_account:{provider}",
                )
                sent += 1
        await session.commit()
    return sent


async def check_cost_rising() -> int:
    """检查本月成本相对上月是否上涨超过 30%。"""
    now = datetime.now()
    this_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    previous_end = this_start
    previous_start = (this_start - timedelta(days=1)).replace(day=1)
    async with async_session_factory() as session:
        this_cost = (await session.execute(select(func.coalesce(func.sum(UsageLog.cost_estimate), 0)).where(UsageLog.created_at >= this_start))).scalar_one()
        previous_cost = (await session.execute(select(func.coalesce(func.sum(UsageLog.cost_estimate), 0)).where(UsageLog.created_at >= previous_start, UsageLog.created_at < previous_end))).scalar_one()
        if previous_cost and this_cost >= previous_cost * 1.3:
            change = (this_cost - previous_cost) / previous_cost
            await send_alert(
                f"本月成本 ¥{this_cost:.2f}，上月 ¥{previous_cost:.2f}，环比上涨 {change:.0%}，建议查看周报",
                key="cost_rising:monthly",
            )
            await session.commit()
            return 1
        await session.commit()
    return 0


async def check_all_alerts() -> int:
    """执行 V4.4 四类告警检查。"""
    total = 0
    for checker in (check_quota_alerts, check_no_active_accounts, check_cost_rising):
        try:
            total += await checker()
        except Exception:
            logger.exception("V4.4 告警检查失败: %s", checker.__name__)
    return total
