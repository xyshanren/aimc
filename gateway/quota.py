"""P0-6 (2026-08-14) 多维限额检查 — V6.6 (2026-09-16) 起账号配额 = 纯真实测量

设计 (V6.6 重设计, 替代原「预留 estimated_tokens + used_value 结算」两段式):
- 原设计缺陷: 结算半边 increment_usage 从未接线 (零调用点), account_quota_usage
  恒空 → 预检退化为纯静态门 max_tokens×2 > limit。tpm (60s 短窗) 对大 max_tokens
  请求恒拒 — deepseek_01/glm_01/stepfun_01 三行 tpm 全中招 (前两行 9/14、后两行
  9/16 已删行止血, 见 docs/dev/architecture.md 决策表)。
- 新设计: token (tpm/daily/weekly/5h) 与 rpm 维度直接对 usage_logs 按
  账号 + 窗口真实 SUM/COUNT (沿用 V4.8 用户配额 _sum_user_tokens 模式)。
  无预留、无 used_value、无静态假门 — 128k max_tokens 的请求真实输出常只有
  几千 token, 吞吐门本就不能预押。
- concurrent 维度: 单进程内存 in-flight 计数 (proxy 转发前 concurrent_acquire,
  上游响应完成/流结束 concurrent_release), 进程重启归零属预期 (in-flight 本就是
  瞬态); 限额值仍配在 account_quotas (dimension=concurrent)。
- balance_cny 维度: 不变 (依赖 account_quota_usage.window_start 作 sync 标记)。

限额检查返回 (allowed: bool, reason: str) 让 caller 决定怎么处理
- allowed=False 时 caller 应该 try_next_account (routes.py 已 failover + 落库)

V4.8 (2026-08-19): per-user 日/月 token 限额 — 真实 SUM + 准入预留, 语义不变
(预算门可以预押一笔估算, 吞吐门不行)。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Account, AccountQuota, AccountQuotaUsage, UsageLog, User

logger = logging.getLogger(__name__)


# ====== 并发 in-flight 计数 (内存, 单进程) ======

_INFLIGHT: dict[int, int] = {}


def concurrent_acquire(account_id: int) -> None:
    """V6.6: proxy 转发上游前 +1 (请求开始)"""
    _INFLIGHT[account_id] = _INFLIGHT.get(account_id, 0) + 1


def concurrent_release(account_id: int) -> None:
    """V6.6: 上游响应完成/流结束后 -1 (幂等: 多释放不会变负)"""
    n = _INFLIGHT.get(account_id, 0)
    if n <= 1:
        _INFLIGHT.pop(account_id, None)
    else:
        _INFLIGHT[account_id] = n - 1


def concurrent_inflight(account_id: int) -> int:
    """当前 in-flight 数 (预检读这个, 不读库)"""
    return _INFLIGHT.get(account_id, 0)


# ====== 限额检查 ======

async def check_account_quota(
    session: AsyncSession, account_id: int
) -> tuple[bool, Optional[str]]:
    """P0-6: 检查 account 所有限额维度 (V6.6: 纯真实测量)

    Args:
        session: AsyncSession
        account_id: 账号 ID

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
        ok, reason = await _check_dimension(session, account_id, q)
        if not ok:
            return False, reason

    return True, None


async def _check_dimension(
    session: AsyncSession, account_id: int, quota: AccountQuota
) -> tuple[bool, Optional[str]]:
    """检查单个维度 (V6.6: 全部真实测量, 无 estimated_tokens 预留)"""
    dim = quota.dimension

    if dim == "balance_cny":
        # 余额是永久值, 直接读
        usage = await _get_or_create_usage(session, account_id, dim)
        # P1-15 fix (2026-08-15): 余额只在 "sync 过" 时检查
        # - balance_cny 需要定期从上游 sync (e.g. deepseek 余额查询 API)
        # - 未 sync 时 used_value=0 (初始值), 不能当作 "余额耗尽"
        # - 用 window_start 作为 "是否 sync 过" 标记:
        #   * window_start IS NULL = 从未 sync -> 跳过 (不拦截, 视作未知)
        #   * window_start 非空 = sync 过 -> used_value 是余额
        # 2026-10-20: 语义从 "耗尽 (<=0) 才拦" 改为 "低于阈值 (limit_value) 即拦" —
        # limit_value=余额下限 (防打爆缓冲), e.g. deepseek 5 元: sync 到余额
        # <=5 时停止路由该账号。原 <=0 语义下 limit_value 不参与判断,
        # "控制到 5 元" 无法表达; 且耗尽才拦时上游往往已经开始拒单。
        # 注意: sync 快照不含 sync 之后的消费 (AIMC 不计上游扣费), 快照偏乐观 —
        # sync 间隔越短越准 (scheduler balance_sync 10min)。
        if usage.window_start is None:
            return True, None  # 未 sync, 不拦截 (余额未知)
        if usage.used_value <= (quota.limit_value or 0):
            return False, (
                f"账户余额低于阈值 (sync 余额 {usage.used_value:.2f} 元 / "
                f"下限 {quota.limit_value:.2f} 元)"
            )
        return True, None

    if dim == "rpm":
        # V6.6: 真实计数 — 近 60s 该账号请求数 (usage_logs 每次转发都落一行)
        since = datetime.now() - timedelta(seconds=60)
        count = await _count_account_requests(session, account_id, since)
        if count >= quota.limit_value:
            return False, f"RPM 超额 (近60s 已 {count} 次 / 限额 {quota.limit_value:.0f})"
        return True, None

    if dim == "concurrent":
        # V6.6: 内存 in-flight 计数 (proxy concurrent_acquire/release 维护)
        inflight = concurrent_inflight(account_id)
        if inflight >= quota.limit_value:
            return False, f"并发超限 (in-flight {inflight} / 限额 {quota.limit_value:.0f})"
        return True, None

    if dim in ("tpm", "daily_tokens", "weekly_tokens", "5h_tokens"):
        # V6.6: 真实测量 — usage_logs 按账号+窗口 SUM(input+output), 无预留。
        # 吞吐门 (tpm) 不能预押: 大 max_tokens 请求的真实消耗远小于声明值。
        since = _token_window_start(quota)
        used = await _sum_account_tokens(session, account_id, since)
        if used > quota.limit_value:
            return False, (
                f"{dim} 实测超额 (窗口内已 {used:.0f} / 限额 {quota.limit_value:.0f})"
            )
        return True, None

    # 未知维度: 不拦 (与旧行为一致 — 旧实现未知维度也是落到末尾 return True)
    return True, None


def _token_window_start(quota: AccountQuota) -> datetime:
    """token 维度统计窗口起点 (V6.6: 每次检查现算, 不再维护窗口状态行)"""
    dim = quota.dimension
    now = datetime.now()
    if dim == "tpm":
        return now - timedelta(seconds=60)
    if dim == "daily_tokens":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if dim == "weekly_tokens":
        if quota.reset_strategy == "rolling" and quota.window_seconds:
            return now - timedelta(seconds=quota.window_seconds)
        monday = now - timedelta(days=now.weekday())
        return monday.replace(hour=0, minute=0, second=0, microsecond=0)
    if dim == "5h_tokens":
        seconds = quota.window_seconds or 5 * 3600
        return now - timedelta(seconds=seconds)
    # 未知 token 维: 有 window_seconds 用滚动窗, 否则近 24h 兜底
    return now - timedelta(seconds=quota.window_seconds or 86400)


async def _sum_account_tokens(session: AsyncSession, account_id: int, since: datetime) -> int:
    """V6.6: 账号窗口内真实 token 消耗 (input+output, NULL 记 0)

    沿用 V4.8 用户配额 _sum_user_tokens 的查询模式 (真实 SUM, 不建累计状态)。
    """
    result = await session.execute(
        select(func.coalesce(func.sum(
            func.coalesce(UsageLog.input_tokens, 0)
            + func.coalesce(UsageLog.output_tokens, 0)
        ), 0)).where(
            UsageLog.account_id == account_id,
            UsageLog.created_at >= since,
        )
    )
    total = result.scalar()
    return int(total) if total is not None else 0


async def _count_account_requests(session: AsyncSession, account_id: int, since: datetime) -> int:
    """V6.6: 账号窗口内真实请求数 (usage_logs 每次转发落一行, 失败行也计)"""
    result = await session.execute(
        select(func.count()).select_from(UsageLog).where(
            UsageLog.account_id == account_id,
            UsageLog.created_at >= since,
        )
    )
    return int(result.scalar() or 0)


async def _get_or_create_usage(
    session: AsyncSession, account_id: int, dimension: str
) -> AccountQuotaUsage:
    """读或 lazy-init usage 行 (V6.6 起仅 balance_cny 的 sync 标记在用)"""
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


# ============================================================
# V4.8 (2026-08-19): per-user 日/月 token 限额 (语义不变)
# ============================================================

async def check_user_quota(
    session: AsyncSession, caller_id: int, estimated_tokens: int = 0
) -> tuple[bool, Optional[str]]:
    """V4.8: 检查 user 日/月 token 限额

    真实 SUM (usage_logs) + 准入预留 estimated_tokens。用户预算门保留预留是
    有意的: 预算是长窗累计, 预押一笔估算可防单笔大请求击穿剩余额度; 账号侧
    吞吐门 (tpm) 则不能预押 (V6.6 起已去掉)。

    Args:
        session: AsyncSession
        caller_id: users.id (middleware 设的 request.state.caller_id)
        estimated_tokens: 本请求预估 token 数 (max_tokens * 2)

    Returns:
        (allowed, reason) — allowed=False 时 reason 描述哪个维度超额

    限额语义:
        - daily_token_limit / monthly_token_limit 为 NULL → 不限 (跳过)
        - 为 0 → 禁用 (直接拒绝)
        - 正整数 → 已用 + estimated > limit 时拒绝
    """
    user = (await session.execute(
        select(User).where(User.id == caller_id)
    )).scalars().first()
    if user is None:
        # user 不存在 (可能已删除), 放行让后续流程处理
        return True, None

    daily_limit = user.daily_token_limit
    monthly_limit = user.monthly_token_limit

    # 两者都 NULL = 不限
    if daily_limit is None and monthly_limit is None:
        return True, None

    now = datetime.now()

    # --- 日限额 ---
    if daily_limit is not None:
        if daily_limit == 0:
            return False, f"日限额已禁用 (daily_token_limit=0)"
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        used = await _sum_user_tokens(session, caller_id, today_start)
        if used + estimated_tokens > daily_limit:
            return False, (
                f"日限额超额 ({used}+{estimated_tokens} > {daily_limit})"
            )

    # --- 月限额 ---
    if monthly_limit is not None:
        if monthly_limit == 0:
            return False, f"月限额已禁用 (monthly_token_limit=0)"
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        used = await _sum_user_tokens(session, caller_id, month_start)
        if used + estimated_tokens > monthly_limit:
            return False, (
                f"月限额超额 ({used}+{estimated_tokens} > {monthly_limit})"
            )

    return True, None


async def _sum_user_tokens(
    session: AsyncSession, caller_id: int, since: datetime
) -> int:
    """V4.8: 查 caller_id 自 since 以来的 input+output token 总量

    coalesce 处理 NULL token (failed/no-usage 请求), 索引 idx_usage_caller_time
    保证查询效率。
    """
    result = await session.execute(
        select(
            func.coalesce(func.sum(
                func.coalesce(UsageLog.input_tokens, 0)
                + func.coalesce(UsageLog.output_tokens, 0)
            ), 0)
        ).where(
            UsageLog.caller_id == caller_id,
            UsageLog.created_at >= since,
        )
    )
    total = result.scalar()
    return int(total) if total is not None else 0