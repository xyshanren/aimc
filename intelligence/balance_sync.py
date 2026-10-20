"""账户余额定期 sync (2026-10-20)

balance_cny 余额门的配套执行体。8/14 设计里 "定期 sync" 只写了注释,
sync 脚本从未存在 — 三行 balance_cny 全部 window_start=NULL 恒不拦截
(装饰性)。本次激活 deepseek 渠道 (用户拍板: 仅 deepseek 适合余额门,
glm/stepfun 订阅为主不适用, siliconflow 代金券未用完暂不激活)。

机制:
- 对所有 enabled=1 且 dimension='balance_cny' 且所属渠道有 balance API
  实现的账号, 调上游余额查询 → upsert account_quota_usage:
  window_start=now (sync 标记), used_value=余额, last_updated=now
- 当前仅实现 deepseek_official (GET /user/balance, 官方按量付费,
  余额单调递减, 快照偏乐观 — sync 间隔 10min 足够)
- 余额门语义 (gateway/quota.py): sync 余额 <= limit_value 即拦

调度: intelligence/scheduler.py 注册 balance_sync job (10min);
首次启动即跑一次 (next_run_time=now), 让门在重启后立即生效。
"""
from __future__ import annotations

import logging
from datetime import datetime

import httpx
from sqlalchemy import select

from db.engine import async_session_factory
from db.models import Account, AccountQuota, AccountQuotaUsage, Channel
from security.crypto import try_decrypt
from config import settings

logger = logging.getLogger(__name__)

# 渠道名 -> 余额查询实现 (返回 CNY 余额 float; 返回 None = 查询失败/不支持, 跳过)
# 2026-10-20 仅 deepseek; 其他渠道待用户逐个拍板再接
_BALANCE_FETCHERS: dict[str, str] = {
    # channel_name -> API base (余额端点 = base 去掉 /v1 + /user/balance)
    "deepseek_official": "https://api.deepseek.com",
}

BALANCE_SYNC_INTERVAL_MINUTES = 10
_BALANCE_TIMEOUT = 15.0


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


async def sync_account_balances() -> int:
    """sync 所有已激活 balance_cny 门的账号余额 → account_quota_usage

    Returns: 成功 sync 的账号数 (供日志/测试)
    """
    synced = 0
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

            usage = await session.get(AccountQuotaUsage, (acc.id, "balance_cny"))
            if usage is None:
                usage = AccountQuotaUsage(account_id=acc.id, dimension="balance_cny")
                session.add(usage)
            usage.window_start = datetime.now()  # sync 标记 (quota.py 据此判断已激活)
            usage.used_value = balance
            usage.last_updated = datetime.now()
            synced += 1
            logger.info(f"[balance_sync] {ch.name}/{acc.account_name} 余额 {balance:.2f} CNY")

        await session.commit()
    return synced
