"""V4.9 (2026-08-25): Channel 智能探活

替代 V4.8.7 P7 的「失败→ 60s 后路由重试」机制。状态机驱动的智能探活:

- 只对「到探活时间」的 account 探活 (next_probe_at <= now)
- 不探活 needs_human / manual_disabled (等人工)
- 并发限流 (Semaphore)
- 探活结果回写 cooldown_manager.probe_result → 决定下次探活时间
- 探活记录写 account_health_log 表 (审计用)

设计原则:
- 不要反复撞死 key (needs_human=True 不探)
- 不要打爆上游 (Semaphore 限流 + 指数退避)
- 失败后能恢复 (mark_success 清状态)
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import httpx

from config import settings
from db.engine import async_session_factory
from db.models import Account, AccountHealthLog, Channel
from gateway.cooldown import cooldown_manager
from gateway.error_classifier import ErrorCategory, classify
from sqlalchemy import select

logger = logging.getLogger(__name__)


# V4.8.4 风格的并发限流 (跟 fetcher.py 一致)
_HEALTH_SEMAPHORE: Optional[asyncio.Semaphore] = None


def _get_sem() -> asyncio.Semaphore:
    global _HEALTH_SEMAPHORE
    if _HEALTH_SEMAPHORE is None:
        _HEALTH_SEMAPHORE = asyncio.Semaphore(getattr(settings, 'health_probe_concurrency', 8))
    return _HEALTH_SEMAPHORE


async def _probe_one(account_id: int, base_url: str, api_key: Optional[str]) -> dict:
    """单 channel 探活, 返回 {account_id, success, status_code, latency_ms, error_msg, error_type, error_code}

    用 GET {base}/models 做轻量健康检查 + 鉴权校验
    """
    url = f"{base_url.rstrip('/')}/models"
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=getattr(settings, 'health_probe_timeout', 5.0)) as client:
            r = await client.get(url, headers=headers)
        latency_ms = int((time.monotonic() - start) * 1000)
        response_text = r.text[:500] if hasattr(r, 'text') else ""
        info = classify(r.status_code, response_text, None)
        # success 标准: 不在「错误」分类里
        success = info.category not in (
            ErrorCategory.AUTH_FAILED,
            ErrorCategory.QUOTA_EXHAUSTED,
            ErrorCategory.MODEL_UNSUPPORTED,
            ErrorCategory.UPSTREAM_5XX,
            ErrorCategory.NETWORK_TRANSIENT,
        )
        return {
            "account_id": account_id,
            "success": success,
            "status_code": r.status_code,
            "latency_ms": latency_ms,
            "error_type": info.category.value,
            "error_code": info.error_code,
            "error_msg": info.error_msg[:200],
        }
    except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as e:
        latency_ms = int((time.monotonic() - start) * 1000)
        info = classify(0, None, e)
        return {
            "account_id": account_id,
            "success": False,
            "status_code": None,
            "latency_ms": latency_ms,
            "error_type": info.category.value,
            "error_code": None,
            "error_msg": str(e)[:200],
        }
    except Exception as e:
        latency_ms = int((time.monotonic() - start) * 1000)
        return {
            "account_id": account_id,
            "success": False,
            "status_code": None,
            "latency_ms": latency_ms,
            "error_type": "unknown",
            "error_code": None,
            "error_msg": str(e)[:200],
        }


async def channel_health_probe() -> int:
    """主入口: 探活所有到期的 account_id

    Returns:
        探活成功的 channel 数
    """
    if not getattr(settings, 'health_probe_enabled', True):
        logger.debug("channel_health_probe: 禁用, 跳过")
        return 0

    # 1. 取该探活的 account_id (内存查询)
    due_ids = cooldown_manager.accounts_due_for_probe()
    if not due_ids:
        logger.debug("channel_health_probe: 无待探活 account")
        return 0

    # 2. 查 DB 取 base_url + api_key (ciphertext 解密)
    async with async_session_factory() as session:
        stmt = select(Account).where(Account.id.in_(due_ids))
        result = await session.execute(stmt)
        accounts = list(result.scalars().all())

    if not accounts:
        return 0

    # 3. 解密 api_key + 取 channel.base_url
    from security.crypto import try_decrypt
    targets = []
    for acc in accounts:
        api_key = None
        try:
            if acc.api_key_ciphertext:
                api_key = try_decrypt(acc.api_key_ciphertext, settings.aimc_master_key)
        except Exception as e:
            logger.debug(f"探活: account #{acc.id} key 解密失败: {e}")
        # 取 channel base_url (channel 走 router cache 或现查)
        ch = acc.channel if hasattr(acc, 'channel') and acc.channel else None
        if ch is None:
            # channel 没预加载, 重新查
            async with async_session_factory() as s2:
                ch_result = await s2.execute(select(Channel).where(Channel.id == acc.channel_id))
                ch = ch_result.scalar_one_or_none()
        if ch is None:
            logger.warning(f"探活: account #{acc.id} channel #{acc.channel_id} 不存在, 跳过")
            continue
        base_url = (ch.api_base or ch.base_url or "").strip()
        if not base_url:
            logger.debug(f"探活: account #{acc.id} channel 无 base_url, 跳过")
            continue
        targets.append((acc.id, base_url, api_key))

    # 4. 并发探活 (Semaphore 限流)
    sem = _get_sem()

    async def _probe_with_sem(aid, base, key):
        async with sem:
            return await _probe_one(aid, base, key)

    results = await asyncio.gather(
        *[_probe_with_sem(aid, base, key) for aid, base, key in targets],
        return_exceptions=True,
    )

    # 5. 过滤异常
    clean: list[dict] = []
    for r in results:
        if isinstance(r, Exception):
            logger.warning(f"探活 task 异常: {r}")
            continue
        clean.append(r)

    # 6. 写 AccountHealthLog 表
    async with async_session_factory() as session:
        for r in clean:
            log = AccountHealthLog(
                account_id=r["account_id"],
                triggered_by="scheduled",
                success=1 if r["success"] else 0,
                status_code=r.get("status_code"),
                error_type=r.get("error_type"),
                error_msg=r.get("error_msg"),
                latency_ms=r.get("latency_ms"),
            )
            session.add(log)
        await session.commit()

    # 7. 回写 cooldown_manager (probe_result 决定下次探活时间 / needs_human 升级)
    for r in clean:
        if r["success"]:
            cooldown_manager.probe_result(r["account_id"], success=True)
        else:
            # 构造 ErrorInfo 给 probe_result
            try:
                cat = ErrorCategory(r.get("error_type") or "unknown")
            except ValueError:
                cat = ErrorCategory.UNKNOWN
            info = ErrorInfo(
                category=cat,
                error_code=r.get("error_code"),
                error_msg=r.get("error_msg") or "",
                needs_human=cat in (ErrorCategory.QUOTA_EXHAUSTED, ErrorCategory.AUTH_FAILED,
                                     ErrorCategory.MODEL_UNSUPPORTED),
                suggested_cooldown_seconds=60 if cat == ErrorCategory.UPSTREAM_5XX else 15,
                suggested_probe_after_seconds=300 if cat == ErrorCategory.UPSTREAM_5XX else 60,
            )
            cooldown_manager.probe_result(r["account_id"], success=False, error_info=info)

    ok_count = sum(1 for r in clean if r["success"])
    fail_count = len(clean) - ok_count
    logger.info(f"channel_health_probe: {ok_count} ok / {fail_count} fail (of {len(clean)} probed)")
    return ok_count


async def cooldown_cleanup_task() -> int:
    """V4.9: 周期性清理过期 cooldown entry (调研发现从未被自动调过)

    Returns:
        清理条数
    """
    try:
        cleaned = cooldown_manager.cleanup()
        if cleaned > 0:
            logger.info(f"cooldown_cleanup: 清掉 {cleaned} 条过期 entry")
        return cleaned
    except Exception as e:
        logger.warning(f"cooldown_cleanup 失败: {e}")
        return 0