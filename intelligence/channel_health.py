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
from datetime import datetime
from typing import Optional

import httpx

from config import settings
from db.engine import async_session_factory
from db.models import Account, AccountHealthLog, Channel
from gateway.cooldown import cooldown_manager
from gateway.error_classifier import ErrorCategory, classify
from sqlalchemy import delete, func, select

logger = logging.getLogger(__name__)


# V4.8.4 风格的并发限流 (跟 fetcher.py 一致)
_HEALTH_SEMAPHORE: Optional[asyncio.Semaphore] = None

# V4.9 防护 #1: 共享 httpx.AsyncClient 单例 (连接池复用, 避免每次新建 client)
# 类比 fetcher.py 的浏览器共享模式. 上限 = health_probe_concurrency × 2 (留余量)
_PROBE_CLIENT: Optional[httpx.AsyncClient] = None
# 连接池硬上限 (防探测时连接池爆炸)
_PROBE_MAX_CONNECTIONS = 20


def _get_sem() -> asyncio.Semaphore:
    global _HEALTH_SEMAPHORE
    if _HEALTH_SEMAPHORE is None:
        _HEALTH_SEMAPHORE = asyncio.Semaphore(getattr(settings, 'health_probe_concurrency', 8))
    return _HEALTH_SEMAPHORE


def _get_probe_client() -> httpx.AsyncClient:
    """V4.9 防护: 进程级共享 httpx.AsyncClient 单例
    - 复用 TCP 连接池, 减少握手开销
    - 避免每 60s × N channel 频繁创建/销毁 client
    - timeout 用 settings.health_probe_timeout (默认 5s)
    - limits 限制最大并发连接数, 防探测时连接池爆炸
    """
    global _PROBE_CLIENT
    if _PROBE_CLIENT is None:
        _PROBE_CLIENT = httpx.AsyncClient(
            timeout=getattr(settings, 'health_probe_timeout', 5.0),
            limits=httpx.Limits(
                max_connections=_PROBE_MAX_CONNECTIONS,
                max_keepalive_connections=_PROBE_MAX_CONNECTIONS // 2,
            ),
        )
    return _PROBE_CLIENT


async def close_probe_client() -> None:
    """优雅关闭 client (供 scheduler 关闭时调用)"""
    global _PROBE_CLIENT
    if _PROBE_CLIENT is not None:
        await _PROBE_CLIENT.aclose()
        _PROBE_CLIENT = None


async def _probe_one(account_id: int, base_url: str, api_key: Optional[str]) -> dict:
    """单 channel 探活, 返回 {account_id, success, status_code, latency_ms, error_msg, error_type, error_code}

    用 GET {base}/models 做轻量健康检查 + 鉴权校验
    V4.9: 使用共享 client (不再每次新建)
    """
    url = f"{base_url.rstrip('/')}/models"
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    start = time.monotonic()
    client = _get_probe_client()
    try:
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

    V4.9 防护 #2:
    - 整个 job 有 hard timeout (health_probe_total_timeout 默认 60s), 超时 cancel 所有 task
    - due_ids 有上限 (health_probe_max_batch 默认 50), 超出丢弃 (下个 job 再探)
    - 防止「雪崩」: 长时间网络挂掉时不会积压几千个待探活

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

    # V4.9 防护 #2: due_ids 上限, 防雪崩
    max_batch = getattr(settings, 'health_probe_max_batch', 50)
    if len(due_ids) > max_batch:
        logger.warning(
            f"channel_health_probe: due_ids={len(due_ids)} > {max_batch}, "
            f"只探前 {max_batch} 个 (下个 job 再探剩余)"
        )
        due_ids = due_ids[:max_batch]

    # V4.9 防护 #2: 整个 job hard timeout (默认 60s)
    total_timeout = getattr(settings, 'health_probe_total_timeout', 60.0)

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

    # 4. 并发探活 (Semaphore 限流 + job hard timeout)
    sem = _get_sem()

    async def _probe_with_sem(aid, base, key):
        async with sem:
            return await _probe_one(aid, base, key)

    # V4.9 防护 #2: 整体 hard timeout (asyncio.wait_for)
    try:
        results = await asyncio.wait_for(
            asyncio.gather(
                *[_probe_with_sem(aid, base, key) for aid, base, key in targets],
                return_exceptions=True,
            ),
            timeout=total_timeout,
        )
    except asyncio.TimeoutError:
        logger.warning(
            f"channel_health_probe: 超时 ({total_timeout}s), "
            f"已探 {len(targets)} 个, 取消剩余 task"
        )
        # 超时: 标记所有未返回的 task 为失败
        results = []
        for aid, base, key in targets:
            results.append({
                "account_id": aid,
                "success": False,
                "status_code": None,
                "latency_ms": int(total_timeout * 1000),
                "error_type": ErrorCategory.NETWORK_TRANSIENT.value,
                "error_code": None,
                "error_msg": f"job timeout ({total_timeout}s)",
            })

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


async def account_health_log_cleanup(retention_days: int = 7) -> int:
    """V4.9 防护 #3: 清理 N 天前的 account_health_log (防表无限增长)

    设计:
    - 默认保留 7 天 (够 dashboard 查探活历史 + 周报分析)
    - 每天凌晨 3 点跑一次 (V4.9.1 注册, 见 scheduler.py)
    - 用 created_at < now - N days 批量删
    - 删前先 COUNT 确认, 避免误删整表

    Args:
        retention_days: 保留天数 (默认 7)

    Returns:
        删掉的条数
    """
    from datetime import timedelta
    from db.engine import async_session_factory
    from db.models import AccountHealthLog
    from sqlalchemy import delete

    try:
        cutoff = datetime.now() - timedelta(days=retention_days)
        async with async_session_factory() as session:
            # 先 count (审计用)
            count_stmt = select(func.count()).select_from(AccountHealthLog).where(
                AccountHealthLog.probe_at < cutoff
            )
            to_delete = (await session.execute(count_stmt)).scalar_one()

            if to_delete == 0:
                logger.debug(f"account_health_log_cleanup: 无过期记录 (保留 {retention_days} 天)")
                return 0

            # 批量删 (SQLAlchemy 2.0 delete + RETURNING 也行, 这里简化)
            delete_stmt = delete(AccountHealthLog).where(
                AccountHealthLog.probe_at < cutoff
            )
            result = await session.execute(delete_stmt)
            await session.commit()
            deleted = result.rowcount or to_delete
            logger.info(
                f"account_health_log_cleanup: 删 {deleted} 条 (cutoff={cutoff.isoformat()})"
            )
            return deleted
    except Exception as e:
        logger.warning(f"account_health_log_cleanup 失败: {e}")
        return 0