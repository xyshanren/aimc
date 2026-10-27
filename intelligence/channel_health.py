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
from gateway.error_classifier import ErrorCategory, ErrorInfo, classify
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


# 账号级 GET /models 探活的每渠道超时覆盖 (秒). 缺省用共享 client 的
# settings.health_probe_timeout (5s). 单请求 httpx timeout= 覆盖 client 默认.
# taotoken_coding: 上游 /models 7d 实测 avg 3.2s / max 5.0s 贴默认 5s 超时
# 偶发 network_transient 假阳性 (2026-10-12 探活分析), 放宽到 8s;
# 其余渠道维持默认 5s (死渠道应被快速判失败).
PROBE_TIMEOUT_OVERRIDES: dict[str, float] = {
    "taotoken_coding": 8.0,
}


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


async def _probe_one(
    account_id: int, base_url: str, api_key: Optional[str],
    timeout: Optional[float] = None,
) -> dict:
    """单 channel 探活, 返回 {account_id, success, status_code, latency_ms, error_msg, error_type, error_code}

    用 GET {base}/models 做轻量健康检查 + 鉴权校验
    V4.9: 使用共享 client (不再每次新建)
    timeout: 单请求超时覆盖 (秒), None=用 client 默认 (settings.health_probe_timeout).
             PROBE_TIMEOUT_OVERRIDES 给慢渠道放宽.
    """
    url = f"{base_url.rstrip('/')}/models"
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    start = time.monotonic()
    client = _get_probe_client()
    req_timeout = timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT
    try:
        r = await client.get(url, headers=headers, timeout=req_timeout)
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


def _probe_error_info(r: dict) -> ErrorInfo:
    """探活失败结果 → ErrorInfo (V6.5 从 channel_health_probe 内联抽出, 可单测).

    B1 修复背景: 本模块此前只 import 了 ErrorCategory/classify, 内联构造
    ErrorInfo 抛 NameError → 失败探活永不回写 cooldown_manager (成功分支正常,
    AccountHealthLog 写入在其之前完成, 故仅有 job_health 失败记录可循)。
    """
    try:
        cat = ErrorCategory(r.get("error_type") or "unknown")
    except ValueError:
        cat = ErrorCategory.UNKNOWN
    return ErrorInfo(
        category=cat,
        error_code=r.get("error_code"),
        error_msg=r.get("error_msg") or "",
        needs_human=cat in (ErrorCategory.QUOTA_EXHAUSTED, ErrorCategory.AUTH_FAILED,
                            ErrorCategory.MODEL_UNSUPPORTED),
        suggested_cooldown_seconds=60 if cat == ErrorCategory.UPSTREAM_5XX else 15,
        suggested_probe_after_seconds=300 if cat == ErrorCategory.UPSTREAM_5XX else 60,
    )


# ============================ V6.15: 配额桶恢复探测 ============================
# 背景 (2026-09-28 生产实证): glm_01 5h 池上游 16:44 重置, quota_exhausted 固定
# 1h 冷却空窗 28 分钟; 用户在官方后台手动重置时 AIMC 只能干等冷却走完。
# GET /models 探活零 token, 上游配额耗尽时照样 200 — 感知不到恢复。
# 这里对隔离中的 (account, model) 桶发 1-token 真聊天: 200 = 配额已恢复 → 清桶隔离。
# 节拍由 cooldown_manager._quarantine_probe 管理 (首探 300s, 指数退避至 1800s),
# 有成本 (modelscope 免费次数用完为止), 所以只探 quota 桶、绝不高频。


async def _chat_probe_one(account_id: int, base_url: str, api_key: Optional[str],
                          upstream_model: str) -> dict:
    """单桶恢复探测: 最小聊天请求, 200 即视为配额恢复

    V6.19 (2026-10-27): 结果新增 transient 字段, 区分两类失败 —
    - 上游明确拒绝 (HTTP 4xx/5xx): 真没恢复, 维持原指数退避
    - 探测自身异常 (超时/连接失败): 不确定状态, 上游可能已恢复
      (生产实证: minimax 窗口重置瞬间探测超时被当"未恢复", 白等 30 分钟),
      上游层对 transient 短重试 + 先用 GET /models 轻探活佐证
      (人工 recovery_check 按钮即走 /models, 与恢复强相关)
    """
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": upstream_model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "stream": False,
    }
    start = time.monotonic()
    client = _get_probe_client()
    try:
        # 单请求超时覆盖 client 默认 (health_probe_timeout=5s): 思考型上游出
        # 1 个 token 也可能 >5s, 探测超时会被误判成"未恢复"白退避一轮
        r = await client.post(url, headers=headers, json=payload, timeout=15.0)
        latency_ms = int((time.monotonic() - start) * 1000)
        response_text = r.text[:500] if hasattr(r, "text") else ""
        info = classify(r.status_code, response_text, None)
        return {
            "account_id": account_id,
            "success": r.status_code == 200,
            "status_code": r.status_code,
            "latency_ms": latency_ms,
            "error_type": info.category.value,
            "error_code": info.error_code,
            "error_msg": info.error_msg[:200],
            "transient": False,
        }
    except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as e:
        latency_ms = int((time.monotonic() - start) * 1000)
        info = classify(0, None, e)
        # V6.19: 自身异常 → /models 轻探活佐证 (上游明显恢复时 200)
        models_result = await _probe_one(
            account_id, base_url, api_key,
            timeout=getattr(settings, 'health_probe_timeout', 5.0) + 5.0,
        )
        return {
            "account_id": account_id,
            "success": False,
            "status_code": None,
            "latency_ms": latency_ms + models_result.get("latency_ms", 0),
            "error_type": info.category.value,
            "error_code": None,
            "error_msg": str(e)[:200],
            "transient": True,
            "models_ok": bool(models_result.get("success")),
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
            "transient": True,
        }


async def quota_recovery_probe() -> int:
    """V6.15 主入口: 探测隔离中的配额桶是否已恢复, 返回恢复 (清桶) 数.

    成功 → cooldown_manager.quota_probe_result(success=True) → mark_success
    清桶隔离, 渠道即刻回到路由池; 失败 → 只推探测节拍, 不动 count/needs_human。
    上游模型名从 price_snapshots.raw_model_id 解析 (规范名兜底)。
    """
    if not getattr(settings, 'quota_recovery_probe_enabled', True):
        return 0

    due = cooldown_manager.quota_buckets_due_for_probe()
    if not due:
        return 0

    account_ids = sorted({aid for aid, _m in due})
    async with async_session_factory() as session:
        accounts = list((await session.execute(
            select(Account).where(Account.id.in_(account_ids))
        )).scalars().all())
        acc_by_id = {acc.id: acc for acc in accounts}
        channel_ids = sorted({acc.channel_id for acc in accounts})
        ch_by_id: dict = {}
        if channel_ids:
            ch_by_id = {c.id: c for c in (await session.execute(
                select(Channel).where(Channel.id.in_(channel_ids))
            )).scalars().all()}
        # (channel_id, canonical_model) → 上游 raw 模型名 (探测 body 用上游认识的名字)
        raw_map: dict[tuple[int, str], str] = {}
        model_names = sorted({m for _a, m in due})
        if channel_ids and model_names:
            from db.models import PriceSnapshot
            rows = list((await session.execute(
                select(PriceSnapshot).where(
                    PriceSnapshot.channel_id.in_(channel_ids),
                    PriceSnapshot.model.in_(model_names),
                )
            )).scalars().all())
            for ps in sorted(rows, key=lambda x: x.id):
                key = (ps.channel_id, ps.model)
                if ps.raw_model_id:
                    raw_map[key] = ps.raw_model_id
                elif key not in raw_map:
                    raw_map[key] = ps.model

    from security.crypto import try_decrypt
    targets = []
    for aid, model in due:
        acc = acc_by_id.get(aid)
        ch = ch_by_id.get(acc.channel_id) if acc else None
        if acc is None or ch is None:
            continue
        base_url = (ch.api_base or ch.base_url or "").strip()
        if not base_url:
            continue
        api_key = None
        if acc.api_key_ciphertext:
            try:
                api_key = try_decrypt(acc.api_key_ciphertext, settings.aimc_master_key)
            except Exception as e:
                logger.debug(f"配额恢复探测: account #{aid} key 解密失败: {e}")
        if not api_key:
            api_key = acc.api_key or None  # legacy 明文行兜底
        upstream_model = raw_map.get((ch.id, model), model)
        targets.append((aid, model, base_url, api_key, upstream_model))

    if not targets:
        return 0

    sem = _get_sem()

    async def _probe_with_sem(aid, model, base, key, upstream):
        async with sem:
            return aid, model, await _chat_probe_one(aid, base, key, upstream)

    total_timeout = getattr(settings, 'health_probe_total_timeout', 60.0)
    try:
        results = await asyncio.wait_for(
            asyncio.gather(
                *[_probe_with_sem(*t) for t in targets],
                return_exceptions=True,
            ),
            timeout=total_timeout,
        )
    except asyncio.TimeoutError:
        logger.warning(f"quota_recovery_probe: 超时 ({total_timeout}s), 取消剩余 task")
        results = [
            (aid, model, {
                "account_id": aid, "success": False, "status_code": None,
                "latency_ms": int(total_timeout * 1000),
                "error_type": "network_transient", "error_code": None,
                "error_msg": f"job timeout ({total_timeout}s)",
            })
            for aid, model, *_rest in targets
        ]

    recovered = 0
    logs = []
    for r in results:
        if isinstance(r, Exception):
            logger.warning(f"配额恢复探测 task 异常: {r}")
            continue
        aid, model, probe = r
        logs.append((aid, probe))
        if probe["success"]:
            recovered += 1
        cooldown_manager.quota_probe_result(
            aid, model, success=probe["success"],
            transient=bool(probe.get("transient")),
        )

    if logs:
        async with async_session_factory() as session:
            for aid, probe in logs:
                msg = probe.get("error_msg") or ""
                # V6.19: transient 且 /models 轻探活 200 → 上游大概率已恢复,
                # 把佐证结果附进日志 (观测用, 不据此直接清桶 — 轻探活不证配额)
                if probe.get("transient") and probe.get("models_ok"):
                    msg = (msg + " [models_ok: GET /models 200, 上游可达性已恢复]"
                           )[:200]
                session.add(AccountHealthLog(
                    account_id=aid,
                    triggered_by="quota_recovery",
                    success=1 if probe["success"] else 0,
                    status_code=probe.get("status_code"),
                    error_type=probe.get("error_type"),
                    error_msg=msg,
                    latency_ms=probe.get("latency_ms"),
                ))
            await session.commit()
        logger.info(
            f"quota_recovery_probe: {recovered} recovered / "
            f"{len(logs) - recovered} still quarantined (of {len(logs)})"
        )
    return recovered


async def quota_recovery_probe_task() -> int:
    """V6.15 模块级 task wrapper (供 APScheduler 注册, 不能用 lambda)"""
    return await quota_recovery_probe()


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
        ch = acc.__dict__.get("channel")  # V6.9: 直读实例 dict, 避免懒加载曾在 session 关闭后抛 DetachedInstanceError
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
        targets.append((acc.id, base_url, api_key, ch.name))

    # 4. 并发探活 (Semaphore 限流 + job hard timeout)
    sem = _get_sem()

    def _probe_timeout_for(chname: str) -> Optional[float]:
        return PROBE_TIMEOUT_OVERRIDES.get(chname)

    async def _probe_with_sem(aid, base, key, chname):
        async with sem:
            return await _probe_one(aid, base, key, timeout=_probe_timeout_for(chname))

    # V4.9 防护 #2: 整体 hard timeout (asyncio.wait_for)
    try:
        results = await asyncio.wait_for(
            asyncio.gather(
                *[_probe_with_sem(aid, base, key, chname) for aid, base, key, chname in targets],
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
        for aid, base, key, chname in targets:
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
            cooldown_manager.probe_result(
                r["account_id"], success=False, error_info=_probe_error_info(r)
            )

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
    """V4.9.1 防护 #3: 清理 N 天前的 account_health_log (防表无限增长)

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


async def account_health_log_cleanup_task() -> int:
    """V4.9.1 模块级 task wrapper (供 APScheduler 注册, 不能用 lambda)

    APScheduler 用 SQLAlchemyJobStore 持久化 job, 回调函数必须能被 pickle.
    不能传 lambda, 所以包一层模块级函数.

    从 settings 读 retention_days (default=7, env: AIMC_ACCOUNT_HEALTH_LOG_RETENTION_DAYS)
    """
    retention = getattr(settings, 'account_health_log_retention_days', 7)
    return await account_health_log_cleanup(retention_days=retention)


async def transcript_log_cleanup_task() -> int:
    """V4.9.3 模块级 task wrapper (供 APScheduler 注册, 不能用 lambda)

    清理 request_transcripts 表中超期的记录, 防止诊断数据无限膨胀。
    从 settings 读 retention_hours (default=72, env: AIMC_TRANSCRIPT_LOG_RETENTION_HOURS)
    """
    from datetime import timedelta
    from db.engine import async_session_factory
    from db.models import RequestTranscript
    from sqlalchemy import delete, select, func

    retention_hours = getattr(settings, 'transcript_log_retention_hours', 72)
    cutoff = datetime.now() - timedelta(hours=retention_hours)

    try:
        async with async_session_factory() as session:
            count_stmt = select(func.count()).select_from(RequestTranscript).where(
                RequestTranscript.created_at < cutoff
            )
            to_delete = (await session.execute(count_stmt)).scalar_one()

            if to_delete == 0:
                logger.debug(f"transcript_log_cleanup: 无过期记录 (保留 {retention_hours}h)")
                return 0

            delete_stmt = delete(RequestTranscript).where(
                RequestTranscript.created_at < cutoff
            )
            result = await session.execute(delete_stmt)
            await session.commit()
            deleted = result.rowcount or to_delete
            logger.info(
                f"transcript_log_cleanup: 删 {deleted} 条 (cutoff={cutoff.isoformat()})"
            )
            return deleted
    except Exception as e:
        logger.warning(f"transcript_log_cleanup 失败: {e}")
        return 0