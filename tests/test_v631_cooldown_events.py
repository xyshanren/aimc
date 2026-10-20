"""V6.3.1 瞬时冷却 badge 修复 + cooldown_events 抓现场测试

覆盖 (dashboard/routes.py V6.3.1 部分):
1. GET /api/pending-summary 默认: 瞬时冷却 (cooldown_degraded) 计入 total/items, 且事件流水落库
2. GET /api/pending-summary?exclude_transient=true: cooldown_degraded 不计入 total/items,
   但抓现场仍落库 (顶栏 badge 不为自愈错误报警, 历史留痕)
3. 事件落库去重: 同 episode (cooldown_until 稳定) 双观察者只插一行;
   episode 内再次失败刷新 cooldown_until → 新行
4. GET /api/cooldown-events: hours 窗口过滤 + account_id 过滤 + 序列化结构
5. 无冷却态: 不落事件

跑法: python -m pytest tests/test_v631_cooldown_events.py -v
"""

import asyncio
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).parent.parent.resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ============================================================
# Mock config + 临时 SQLite (同 test_v48_dashboard 模式)
# ============================================================

def _install_mock_env():
    import os
    import tempfile
    import types

    tmp_dir = tempfile.mkdtemp(prefix="aimc_v631_cde_")
    db_path = os.path.join(tmp_dir, "test.db")
    db_url = f"sqlite+aiosqlite:///{db_path}"

    sys.modules.setdefault("config", types.ModuleType("config"))
    sys.modules["config"].settings = types.SimpleNamespace(
        openai_api_key="sk-test",
        openai_base_url="https://mock.local/v1",
        parse_model="gpt-4o-mini",
        parse_confidence_threshold=0.6,
        crawl_enabled=True,
        aimc_master_key="a" * 64,
        route_refresh_interval=600,
        db_url=db_url,
        db_path=db_path,
        host="0.0.0.0",
        port=8080,
        log_level="info",
    )


_install_mock_env()


def _get_client():
    """同步工厂 — 返回 AsyncClient 实例 (非 coroutine), 让 async with 可用."""
    import httpx
    from fastapi import FastAPI
    from dashboard.routes import router as dashboard_router

    app = FastAPI()
    app.include_router(dashboard_router)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


async def _reset_db():
    """建表 + 清空本测试涉及的表 (清表走 sqlite3 内联字面量, 同 test_v63 模式 —
    表名固定枚举非外部输入, FK 子表在前)"""
    import config
    from db.init_db import init_db

    await init_db()
    conn = sqlite3.connect(config.settings.db_path)
    conn.execute("DELETE FROM cooldown_events")
    conn.execute("DELETE FROM usage_logs")
    conn.execute("DELETE FROM api_keys")
    conn.execute("DELETE FROM accounts")
    conn.execute("DELETE FROM channels")
    conn.execute("DELETE FROM users")
    conn.commit()
    conn.close()


def _clear_runtime_state():
    """清全局单例内存态 (冷却 + 路由缓存 + 配额桶), 避免跨测试污染"""
    from gateway.cooldown import cooldown_manager
    from gateway.router import route_engine

    cooldown_manager._state.clear()
    cooldown_manager._quarantine.clear()
    cooldown_manager._quarantine_probe.clear()
    route_engine._channels_cache = []


async def _seed_channel_account():
    """建 Channel + Account (FK 先后), 返回 account_id"""
    from db.engine import async_session_factory
    from db.models import Account, Channel

    async with async_session_factory() as session:
        channel = Channel(name="v631_ch", provider="test",
                          base_url="http://localhost:1/v1", status="active")
        session.add(channel)
        await session.flush()
        acct = Account(channel_id=channel.id, account_name="v631_acct",
                       api_key="", status="active")
        session.add(acct)
        await session.commit()
        return acct.id


def _seed_router_cache(aid):
    """往 route_engine 渠道缓存塞一条, 让 _lookup_channel_info 能取到快照字段"""
    from gateway.router import route_engine

    route_engine._channels_cache = [{
        "account_id": aid,
        "name": "v631_ch",
        "account_name": "v631_acct",
    }]


def _seed_transient_cooldown(aid, error_type="rate_limited", error_code="429",
                             cooldown_secs=45):
    """往全局 cooldown_manager 注入一条瞬时冷却态 (needs_human=False, 在冷却中)"""
    from gateway.cooldown import cooldown_manager

    cooldown_manager._state[aid] = {
        "count": 1,
        "cooldown_until": time.time() + cooldown_secs,
        "first_failure_at": time.time() - 10,
        "last_failure_at": time.time() - 5,
        "last_success_at": None,
        "last_error_type": error_type,
        "last_error_code": error_code,
        "last_error_msg": "rate limit exceeded (mock)",
        "needs_human": False,
        "next_probe_at": time.time() + 30,
        "probe_attempts": 1,
        "manual_disabled_at": None,
    }


def _seed_quarantine_bucket(aid, model="MiniMax-M3.1-Flash", secs=1800):
    """往全局 cooldown_manager 注入一条配额桶隔离态 (账号级不在冷却)"""
    from gateway.cooldown import cooldown_manager

    cooldown_manager._state[aid] = {
        "count": 1,
        "cooldown_until": None,
        "first_failure_at": time.time() - 10,
        "last_failure_at": time.time() - 5,
        "last_success_at": None,
        "last_error_type": "quota_exhausted",
        "last_error_code": "rate_limit_error",
        "last_error_msg": "已达到 Token Plan 用量上限 (mock)",
        "needs_human": False,
        "next_probe_at": None,
        "probe_attempts": 0,
        "manual_disabled_at": None,
    }
    cooldown_manager._quarantine[(aid, model)] = time.time() + secs
    cooldown_manager._quarantine_probe[(aid, model)] = {
        "next_probe_at": time.time() + 300, "attempts": 0,
    }


def _fetch_events():
    """读 cooldown_events 全表 (raw sqlite3, 同 test_v60 读库模式)"""
    import config

    conn = sqlite3.connect(config.settings.db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, account_id, channel_name, account_name, error_type, "
        "error_code, error_msg, cooldown_until, remaining_seconds, created_at "
        "FROM cooldown_events ORDER BY id"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ============================================================
# 1. 默认调用: 瞬时冷却计入 + 事件落库
# ============================================================

def test_default_includes_transient_and_captures_event():
    async def _run():
        await _reset_db()
        _clear_runtime_state()
        aid = await _seed_channel_account()
        _seed_router_cache(aid)
        _seed_transient_cooldown(aid)
        try:
            async with _get_client() as c:
                resp = await c.get("/dashboard/api/pending-summary")
                assert resp.status_code == 200, resp.text
                data = resp.json()
                cats = {i["category"] for i in data["items"]}
                assert "cooldown_degraded" in cats
                assert data["total"] == 1
                item = next(i for i in data["items"]
                            if i["category"] == "cooldown_degraded")
                assert item["count"] == 1
                assert item["severity"] == "warning"
                assert item["label"] == "⚠️ 自动冷却中"

            events = _fetch_events()
            assert len(events) == 1
            ev = events[0]
            assert ev["account_id"] == aid
            assert ev["error_type"] == "rate_limited"
            assert ev["error_code"] == "429"
            assert "rate limit" in (ev["error_msg"] or "")
            assert ev["channel_name"] == "v631_ch"
            assert ev["account_name"] == "v631_acct"
            assert ev["remaining_seconds"] > 0
            assert ev["cooldown_until"] > time.time()
            assert ev["created_at"] is not None
        finally:
            _clear_runtime_state()
    asyncio.run(_run())


# ============================================================
# 2. exclude_transient=true: badge 不数, 但抓现场仍落库
# ============================================================

def test_exclude_transient_still_captures_event():
    async def _run():
        await _reset_db()
        _clear_runtime_state()
        aid = await _seed_channel_account()
        _seed_router_cache(aid)
        _seed_transient_cooldown(aid)
        try:
            async with _get_client() as c:
                resp = await c.get(
                    "/dashboard/api/pending-summary?exclude_transient=true")
                assert resp.status_code == 200, resp.text
                data = resp.json()
                cats = {i["category"] for i in data["items"]}
                assert "cooldown_degraded" not in cats
                assert data["total"] == 0
                assert data["items"] == []

            # 核心设计点: exclude 只影响 badge 计数, 抓现场不跳过
            events = _fetch_events()
            assert len(events) == 1
            assert events[0]["account_id"] == aid
        finally:
            _clear_runtime_state()
    asyncio.run(_run())


# ============================================================
# 3. 去重: 同 episode 一行; cooldown_until 刷新 → 新行
# ============================================================

def test_event_capture_dedup_same_episode():
    async def _run():
        await _reset_db()
        _clear_runtime_state()
        aid = await _seed_channel_account()
        _seed_router_cache(aid)
        _seed_transient_cooldown(aid)
        try:
            # badge 30s + ops 60s 双观察者模拟: 连续两次调用
            async with _get_client() as c:
                r1 = await c.get("/dashboard/api/pending-summary")
                assert r1.status_code == 200
                r2 = await c.get("/dashboard/api/pending-summary")
                assert r2.status_code == 200

            events = _fetch_events()
            assert len(events) == 1  # 同 episode 不重复

            # episode 内再次失败 → cooldown_until 刷新 → 新行 (记录刷新事实)
            _seed_transient_cooldown(aid, cooldown_secs=120)
            async with _get_client() as c:
                r3 = await c.get("/dashboard/api/pending-summary")
                assert r3.status_code == 200

            events = _fetch_events()
            assert len(events) == 2
            untils = sorted(e["cooldown_until"] for e in events)
            assert untils[1] - untils[0] > 1  # 两次观察值确实不同
        finally:
            _clear_runtime_state()
    asyncio.run(_run())


# ============================================================
# 4. GET /api/cooldown-events: 窗口/账号过滤 + 序列化
# ============================================================

def test_cooldown_events_endpoint_filters():
    async def _run():
        await _reset_db()
        _clear_runtime_state()
        aid = await _seed_channel_account()
        try:
            from db.engine import async_session_factory
            from db.models import CooldownEvent

            async with async_session_factory() as session:
                # 一条 25h 前 (超 24h 窗口), 一条新鲜
                session.add(CooldownEvent(
                    account_id=aid, channel_name="v631_ch", account_name="v631_acct",
                    error_type="upstream_5xx", error_code="502",
                    error_msg="bad gateway",
                    cooldown_until=time.time() + 30, remaining_seconds=30.0,
                    created_at=datetime.now() - timedelta(hours=25),
                ))
                session.add(CooldownEvent(
                    account_id=aid, channel_name="v631_ch", account_name="v631_acct",
                    error_type="rate_limited", error_code="429",
                    error_msg="too many requests",
                    cooldown_until=time.time() + 45, remaining_seconds=45.0,
                ))
                await session.commit()

            async with _get_client() as c:
                resp = await c.get("/dashboard/api/cooldown-events?hours=24")
                assert resp.status_code == 200, resp.text
                data = resp.json()
                assert data["total"] == 1
                ev = data["events"][0]
                assert ev["error_type"] == "rate_limited"
                assert ev["error_code"] == "429"
                assert ev["channel_name"] == "v631_ch"
                assert ev["account_id"] == aid
                assert ev["created_at"] is not None
                # 新鲜的排在前面 (created_at desc)
                assert ev["remaining_seconds"] == 45.0

                resp2 = await c.get("/dashboard/api/cooldown-events?hours=48")
                assert resp2.status_code == 200
                assert resp2.json()["total"] == 2

                resp3 = await c.get(
                    "/dashboard/api/cooldown-events?hours=48&account_id=99999")
                assert resp3.status_code == 200
                assert resp3.json()["total"] == 0
        finally:
            _clear_runtime_state()
    asyncio.run(_run())


# ============================================================
# 5. 无冷却态: 不落事件
# ============================================================

def test_no_cooldown_no_events_written():
    async def _run():
        await _reset_db()
        _clear_runtime_state()
        await _seed_channel_account()  # 有账号但无冷却态
        try:
            async with _get_client() as c:
                resp = await c.get("/dashboard/api/pending-summary")
                assert resp.status_code == 200, resp.text
                assert resp.json() == {"total": 0, "items": []}

            assert _fetch_events() == []
        finally:
            _clear_runtime_state()
    asyncio.run(_run())


# ============================================================
# 6. V6.17: 配额桶隔离 (订阅窗口耗尽) 也进待处理栏
# ============================================================

def test_quota_bucket_category_visible():
    """V6.17: 订阅窗口耗尽走模型桶隔离 (账号级不冷却) → 待处理栏必须有条目,
    否则该状态完全不可见, 用户既看不到也手动放行不了。"""
    async def _run():
        await _reset_db()
        _clear_runtime_state()
        aid = await _seed_channel_account()
        _seed_router_cache(aid)
        _seed_quarantine_bucket(aid)
        try:
            async with _get_client() as c:
                resp = await c.get("/dashboard/api/pending-summary")
                assert resp.status_code == 200, resp.text
                data = resp.json()
                cats = {i["category"] for i in data["items"]}
                assert "quota_bucket" in cats
                item = next(i for i in data["items"]
                            if i["category"] == "quota_bucket")
                assert item["count"] == 1
                assert item["severity"] == "warning"
                assert item["label"] == "⏳ 配额桶隔离中"
                ex = item["examples"][0]
                assert ex["account_id"] == aid
                assert ex["channel"] == "v631_ch"
                assert "配额桶隔离中" in ex["error_msg"]
                assert "MiniMax-M3.1-Flash" in ex["error_msg"]
        finally:
            _clear_runtime_state()
    asyncio.run(_run())


def test_quota_bucket_excluded_when_needs_human():
    """needs_human 账号已由「余额不足」类目呈现, 桶条目不重复出现"""
    async def _run():
        await _reset_db()
        _clear_runtime_state()
        aid = await _seed_channel_account()
        _seed_router_cache(aid)
        _seed_quarantine_bucket(aid)
        from gateway.cooldown import cooldown_manager
        cooldown_manager._state[aid]["needs_human"] = True
        try:
            async with _get_client() as c:
                resp = await c.get("/dashboard/api/pending-summary")
                assert resp.status_code == 200, resp.text
                cats = {i["category"] for i in resp.json()["items"]}
                assert "quota_bucket" not in cats
        finally:
            _clear_runtime_state()
    asyncio.run(_run())
