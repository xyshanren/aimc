"""P0-6 (2026-08-14): 多维限额检查 + 累加 (router pre-check / proxy post-success)

设计:
- router pre-check: 请求前按 (account, dimension) 检查, 超额则跳过该 account
- proxy post-success: 成功后按 response.usage 累加 (input + output tokens)
- 并发维度 (concurrent): 请求开始 +1, 结束 -1 (用 asyncio context manager)

rolling window:
- 维护 window_start 时间戳, 检查 "now - window_start > window_seconds" 时 roll over
- 简化实现: usage_logs 表里有 timestamp, 可以查过去 N 秒的 sum
- 当前实现: window_start + 累计 used_value (粗略, 不精确 sliding 但够用)

限额检查返回 (allowed: bool, reason: str) 让 caller 决定怎么处理
- allowed=False 时 caller 应该 try_next_account
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Account, AccountQuota, AccountQuotaUsage

logger = logging.getLogger(__name__)


# ====== 限额检查 ======

async def check_account_quota(
    session: AsyncSession, account_id: int, estimated_tokens: int = 0
) -> tuple[bool, Optional[str]]:
    """P0-6: 检查 account 是否所有限额维度都在预算内

    Args:
        session: AsyncSession
        account_id: 账号 ID
        estimated_tokens: 估算本请求消耗的 token 数 (用于 daily/weekly/5h 维度预判)

    Returns:
        (allowed, reason) — allowed=False 时 reason 描述哪个维度超额
    """
    quotas = (await session.execute(
        select(AccountQuota).where(
            AccountQuota.account_id == account_id,
            AccountQuota.enabled == 1,
        )
    )).scalars().all()

    if not quotas:
        return True, None  # 没配置 = 不限

    for q in quotas:
        ok, reason = await _check_dimension(session, account_id, q, estimated_tokens)
        if not ok:
            return False, reason

    return True, None


async def _check_dimension(
    session: AsyncSession, account_id: int, quota: AccountQuota, estimated_tokens: int
) -> tuple[bool, Optional[str]]:
    """检查单个维度"""
    dim = quota.dimension

    if dim == "balance_cny":
        # 余额是永久值, 直接读
        usage = await _get_or_create_usage(session, account_id, dim)
        # P1-15 fix (2026-08-15): 余额只在 "sync 过" 时检查
        # - balance_cny 需要定期从上游 sync (e.g. deepseek 余额查询 API)
        # - 未 sync 时 used_value=0 (初始值), 不能当作 "余额耗尽"
        # - 用 window_start 作为 "是否 sync 过" 标记:
        #   * window_start IS NULL = 从未 sync -> 跳过 (不拦截, 视作未知)
        #   * window_start 非空 = sync 过 -> used_value 是余额, <=0 拦截
        # - 幂等: sync 脚本设置 window_start=CURRENT_TIMESTAMP + used_value=余额
        if usage.window_start is None:
            return True, None  # 未 sync, 不拦截 (余额未知)
        if usage.used_value <= 0:
            return False, f"账户余额耗尽 ({usage.used_value:.2f} 元)"
        return True, None

    elif dim == "rpm":
        # RPM: 当前分钟内已用请求数
        usage = await _get_or_create_usage(session, account_id, dim)
        now = datetime.now()
        if usage.window_start is None or (
            now - usage.window_start
        ).total_seconds() > 60:
            # rollover
            usage.window_start = now
            usage.used_value = 0
        if usage.used_value >= quota.limit_value:
            return False, f"RPM 超额 ({usage.used_value:.0f}/{quota.limit_value:.0f})"

    elif dim == "concurrent":
        # 并发: in-flight 请求数
        usage = await _get_or_create_usage(session, account_id, dim)
        if usage.used_value >= quota.limit_value:
            return False, f"并发超限 ({usage.used_value:.0f}/{quota.limit_value:.0f})"

    elif dim in ("daily_tokens", "weekly_tokens", "5h_tokens"):
        # Token 限额: 窗口内累计 used_value + estimated_tokens
        usage = await _get_or_create_usage(session, account_id, dim)
        if usage.window_start is None or _is_window_expired(usage, quota):
            _rollover_window(usage, quota)
        if usage.used_value + estimated_tokens > quota.limit_value:
            return False, (
                f"{dim} 超额 ({usage.used_value:.0f}+{estimated_tokens} > "
                f"{quota.limit_value:.0f})"
            )

    elif dim == "tpm":
        # TPM: 当前分钟内已用 token 数
        usage = await _get_or_create_usage(session, account_id, dim)
        now = datetime.now()
        if usage.window_start is None or (
            now - usage.window_start
        ).total_seconds() > 60:
            usage.window_start = now
            usage.used_value = 0
        if usage.used_value + estimated_tokens > quota.limit_value:
            return False, f"TPM 超额 ({usage.used_value:.0f}+{estimated_tokens} > {quota.limit_value:.0f})"

    return True, None


def _is_window_expired(usage: AccountQuotaUsage, quota: AccountQuota) -> bool:
    """窗口是否过期 (需要 rollover)"""
    if usage.window_start is None:
        return True
    if quota.reset_strategy == "rolling":
        # rolling: window_seconds 前到现在的请求算窗口
        # 简化: window_start 是窗口起点, 过期 = now - window_start > window_seconds
        if quota.window_seconds is None:
            return False  # 没设窗口 = 不过期
        return (datetime.now() - usage.window_start).total_seconds() > quota.window_seconds
    elif quota.reset_strategy == "fixed_daily":
        # daily: 跨天就 rollover
        now = datetime.now()
        return usage.window_start.date() < now.date()
    elif quota.reset_strategy == "fixed_weekly":
        # weekly: 跨 ISO 周就 rollover
        now = datetime.now()
        return usage.window_start.isocalendar()[1] != now.isocalendar()[1]
    return False


def _rollover_window(usage: AccountQuotaUsage, quota: AccountQuota) -> None:
    """窗口过期时重置"""
    now = datetime.now()
    if quota.reset_strategy == "rolling":
        usage.window_start = now
        usage.used_value = 0
    elif quota.reset_strategy == "fixed_daily":
        usage.window_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        usage.used_value = 0
    elif quota.reset_strategy == "fixed_weekly":
        # 周一 00:00 为起点
        days_since_monday = now.weekday()
        week_start = now - timedelta(days=days_since_monday)
        usage.window_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)
        usage.used_value = 0


async def _get_or_create_usage(
    session: AsyncSession, account_id: int, dimension: str
) -> AccountQuotaUsage:
    """P0-6: 读或 lazy-init usage 行"""
    usage = (await session.execute(
        select(AccountQuotaUsage).where(
            AccountQuotaUsage.account_id == account_id,
            AccountQuotaUsage.dimension == dimension,
        )
    )).scalars().first()
    if not usage:
        usage = AccountQuotaUsage(
            account_id=account_id,
            dimension=dimension,
            used_value=0,
            window_start=None,
        )
        session.add(usage)
        await session.flush()
    return usage


# ====== 累加 (proxy 成功后调用) ======

async def increment_usage(
    session: AsyncSession,
    account_id: int,
    dimension: str,
    amount: float,
) -> None:
    """P0-6: 累加 usage (proxy 成功后调用)"""
    usage = await _get_or_create_usage(session, account_id, dimension)
    usage.used_value += amount
    usage.last_updated = datetime.now()
    await session.commit()


async def decrement_concurrent(
    session: AsyncSession, account_id: int, amount: float = 1.0
) -> None:
    """P0-6: 释放并发计数 (请求结束时调用)"""
    await increment_usage(session, account_id, "concurrent", -amount)
