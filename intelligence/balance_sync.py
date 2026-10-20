"""账户余额 sync (2026-10-20)

balance_cny 余额门的配套执行体。8/14 设计里 "定期 sync" 只写了注释,
sync 脚本从未存在 — 三行 balance_cny 全部 window_start=NULL 恒不拦截
(装饰性)。本次激活 deepseek 渠道 (用户拍板: 仅 deepseek 适合余额门,
glm/stepfun 订阅为主不适用, siliconflow 代金券未用完暂不激活)。

机制:
- 对所有 enabled=1 且 dimension='balance_cny' 且所属渠道有 balance API
  实现的账号, 调上游余额查询 → upsert account_quota_usage:
  window_start=now (sync 标记), used_value=余额, last_updated=now
- 当前仅实现 deepseek_official (GET /user/balance, 官方按量付费,
  余额单调递减)
- 余额门语义 (gateway/quota.py): sync 余额 <= limit_value 即拦

调度 (2026-10-20 升级为自适应):
- scheduler 注册 1 分钟 tick 的 job, 但内部按"烧钱速率"决定本轮是否真 sync —
  固定 10 分钟对空闲渠道是纯噪声 (生产实证: deepseek 近 7 天零流量,
  余额 27.56 元一动不动, 每 10 分钟打一次上游接口毫无收益)
- 自适应规则 (纯函数 plan_sync, 可单测):
    1. burn_rate = 近 60 分钟 usage_logs 的 cost_estimate 之和 (元/秒)
    2. 当前余额推算 = 上次 sync 值 - burn_rate * 已过秒数
    3. headroom = 推算余额 - 余额下限 (limit_value)
    4. 期望间隔 = SAFETY_RATIO * headroom / burn_rate  (快照误差只允许占
       headroom 的 SAFETY_RATIO), clamp 到 [2 分钟, 6 小时]
    5. burn_rate = 0 (无流量) → 6 小时心跳
- 效果: 空闲渠道 6h 一次; 流量起来后自动加密; 逼近余额下限时自动变成
  2 分钟一次 (临门一脚的精度), 远离下限时拉长
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

import httpx
from sqlalchemy import select

from db.engine import async_session_factory
from db.models import Account, AccountQuota, AccountQuotaUsage, Channel, UsageLog
from security.crypto import try_decrypt
from config import settings

logger = logging.getLogger(__name__)

# 渠道名 -> 余额查询实现 (返回 CNY 余额 float; 返回 None = 查询失败/不支持, 跳过)
# 2026-10-20 仅 deepseek; 其他渠道待用户逐个拍板再接
_BALANCE_FETCHERS: dict[str, str] = {
    # channel_name -> API base (余额端点 = base 去掉 /v1 + /user/balance)
    "deepseek_official": "https://api.deepseek.com",
}

# scheduler tick 间隔 (内部再按 burn rate 决定真 sync 与否)
BALANCE_SYNC_TICK_MINUTES = 1
# 自适应参数
MIN_INTERVAL_SECONDS = 120          # 最短 2 分钟 (逼近下限时)
MAX_INTERVAL_SECONDS = 6 * 3600     # 最长 6 小时 (空闲心跳)
SAFETY_RATIO = 0.2                 # 允许的快照误差 / headroom
BURN_WINDOW_MINUTES = 60           # 烧钱速率统计窗口
_BALANCE_TIMEOUT = 15.0


def plan_sync(
    used_value: float,
    limit_value: float,
    elapsed_sec: float,
    burn_rate_cny_per_sec: float,
) -> tuple[bool, float, float]:
    """纯函数: 决定本轮是否 sync + 下次间隔 + 推算的当前余额

    Args:
        used_value: 上次 sync 到的余额 (元)
        limit_value: 余额下限 (元), 低于即拦
        elapsed_sec: 距上次 sync 已过秒数
        burn_rate_cny_per_sec: 近期烧钱速率 (元/秒), 0 = 无流量

    Returns:
        (due, interval_sec, estimated_current_balance)
    """
    if burn_rate_cny_per_sec <= 0:
        # 无流量 → 余额不动, 6 小时心跳即可
        return elapsed_sec >= MAX_INTERVAL_SECONDS, MAX_INTERVAL_SECONDS, used_value

    estimated_current = used_value - burn_rate_cny_per_sec * elapsed_sec
    headroom = estimated_current - (limit_value or 0)
    if headroom <= 0:
        # 已到/低于下限 → 立刻 sync (拿到真实余额确认是否该拦)
        return True, MIN_INTERVAL_SECONDS, estimated_current

    interval = SAFETY_RATIO * headroom / burn_rate_cny_per_sec
    interval = max(MIN_INTERVAL_SECONDS, min(MAX_INTERVAL_SECONDS, interval))
    return elapsed_sec >= interval, interval, estimated_current


async def fetch_deepseek_balance(api_key: str) -> float | None:
    """DeepSeek 官方余额查询: GET /user/balance → balance_infos[].total_balance (CNY)"""
    async with httpx.AsyncClient(timeout=_BALANCE_TIMEOUT) as client:
        resp = await client.get(
            f"{_BALANCE_FETCHERS['deepseek_official']}/user/balance",
            headers={"Authorization": f"Bearer {api_key}"},
        )
    if resp.status_code != 200:
        logger.warning(f"[balance_sync] deepseek /user/balance -> {resp.status_code}")
        return None
    data = resp.json()
    if not data.get("is_available", True):
        logger.warning("[balance_sync] deepseek is_available=false")
        return None
    for info in data.get("balance_infos", []):
        if info.get("currency") == "CNY":
            return float(info["total_balance"])
    logger.warning("[balance_sync] deepseek 返回无 CNY balance_infos")
    return None


async def _burn_rate_cny_per_sec(session, account_id: int) -> float:
    """近 BURN_WINDOW_MINUTES 的真实花费 (元/秒) — 用 cost_estimate (元) 聚合"""
    from sqlalchemy import func

    since = datetime.now() - timedelta(minutes=BURN_WINDOW_MINUTES)
    total = (await session.execute(
        select(func.coalesce(func.sum(UsageLog.cost_estimate), 0.0)).where(
            UsageLog.account_id == account_id,
            UsageLog.created_at >= since,
        )
    )).scalar() or 0.0
    return float(total) / (BURN_WINDOW_MINUTES * 60)


async def sync_account_balances() -> dict:
    """按 burn rate 自适应 sync 已激活 balance_cny 门的账号余额

    Returns: {"synced": n, "skipped": n, "detail": [...]} (供日志/测试)
    """
    synced = 0
    skipped = 0
    detail: list[dict] = []
    async with async_session_factory() as session:
        rows = (await session.execute(
            select(AccountQuota, Account, Channel)
            .join(Account, Account.id == AccountQuota.account_id)
            .join(Channel, Channel.id == Account.channel_id)
            .where(
                AccountQuota.enabled == 1,
                AccountQuota.dimension == "balance_cny",
                Account.status != "manual_disabled",
                Channel.name.in_(_BALANCE_FETCHERS.keys()),
            )
        )).all()

        for quota, acc, ch in rows:
            usage = await session.get(AccountQuotaUsage, (acc.id, "balance_cny"))
            now = datetime.now()

            # 从未 sync → 立即 sync (门需要 sync 标记才生效)
            due = usage is None or usage.window_start is None
            if not due:
                elapsed = (now - (usage.last_updated or usage.window_start)).total_seconds()
                burn = await _burn_rate_cny_per_sec(session, acc.id)
                due, interval, est = plan_sync(
                    usage.used_value, quota.limit_value or 0, elapsed, burn
                )
                detail.append({
                    "account": acc.account_name,
                    "due": due,
                    "burn_cny_per_sec": round(burn, 8),
                    "est_balance": round(est, 2),
                    "next_in_sec": round(interval),
                })
                if not due:
                    skipped += 1
                    continue

            api_key = None
            if acc.api_key_ciphertext:
                try:
                    api_key = try_decrypt(acc.api_key_ciphertext, settings.aimc_master_key)
                except Exception as e:
                    logger.error(f"[balance_sync] {ch.name}/{acc.account_name} 解密失败: {e}")
                    continue
            if not api_key:
                continue

            balance = await fetch_deepseek_balance(api_key)
            if balance is None:
                continue

            if usage is None:
                usage = AccountQuotaUsage(account_id=acc.id, dimension="balance_cny")
                session.add(usage)
            usage.window_start = datetime.now()  # sync 标记 (quota.py 据此判断已激活)
            usage.used_value = balance
            usage.last_updated = datetime.now()
            synced += 1
            logger.info(f"[balance_sync] {ch.name}/{acc.account_name} 余额 {balance:.2f} CNY")

        await session.commit()

    if detail:
        logger.info(f"[balance_sync] 自适应调度: {detail}")
    logger.info(f"[balance_sync] 本轮 synced={synced} skipped={skipped}")
    return {"synced": synced, "skipped": skipped, "detail": detail}
