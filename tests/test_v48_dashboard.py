"""V4.8 Dashboard 多用户端点测试

覆盖 (dashboard/routes.py V4.8 部分):
1. GET /api/stats/per-user: 空数据 → count=0
2. GET /api/stats/per-user: 有数据 → 按 caller_id 聚合, NULL → "(未鉴权/legacy)"
3. GET /api/stats/per-user?days=N: 时间窗口过滤
4. GET /api/keys: 列出 user + key, 结构正确, key_hash 不出现
5. POST /api/keys/{id}/revoke: active → revoked, 幂等
6. POST /api/keys/{id}/revoke: 不存在 → 404

跑法: python -m pytest tests/test_v48_dashboard.py -v
"""

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent.resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ============================================================
# Mock config + 临时 SQLite (同 test_v46_dashboard_accounts 模式)
# ============================================================

def _install_mock_env():
    import os
    import tempfile
    import types

    tmp_dir = tempfile.mkdtemp(prefix="aimc_v48_dash_")
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


# ============================================================
# TestApp: 把 dashboard router 挂到独立 FastAPI app, 用 ASGITransport
# ============================================================

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
    """初始化表 + 清空 users/api_keys/usage_logs"""
    from db.engine import async_session_factory
    from db.init_db import init_db
    from db.models import User, ApiKey, UsageLog
    from sqlalchemy import delete

    await init_db()
    async with async_session_factory() as session:
        await session.execute(delete(UsageLog))
        await session.execute(delete(ApiKey))
        await session.execute(delete(User))
        await session.commit()


async def _seed_user(factory, username, display_name=None, role="user",
                     daily_limit=None, monthly_limit=None):
    """建一个 user, 返回 user_id"""
    from db.models import User

    async with factory() as session:
        u = User(
            username=username,
            display_name=display_name or username,
            role=role,
            status="active",
            daily_token_limit=daily_limit,
            monthly_token_limit=monthly_limit,
        )
        session.add(u)
        await session.commit()
        return u.id


async def _seed_api_key(factory, user_id, key_plain="aimc-testkey123456",
                        is_admin=0, status="active", label=None):
    """建一个 api_key, 返回 key_id"""
    import hashlib
    from db.models import ApiKey

    async with factory() as session:
        k = ApiKey(
            user_id=user_id,
            key_hash=hashlib.sha256(key_plain.encode()).hexdigest(),
            key_prefix=key_plain[:8],
            is_admin=is_admin,
            status=status,
            label=label,
        )
        session.add(k)
        await session.commit()
        return k.id


async def _seed_usage_log(factory, caller_id, input_tokens=100, output_tokens=50,
                          cost=0.01, created_at=None, model="test-model"):
    """写一条 usage_log, 返回 log_id"""
    from db.models import UsageLog

    async with factory() as session:
        log = UsageLog(
            caller_id=caller_id,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_estimate=cost,
            status_code=200,
            created_at=created_at or datetime.now(),
        )
        session.add(log)
        await session.commit()
        return log.id


# ============================================================
# Test 1: per-user 空数据
# ============================================================

def test_per_user_usage_empty():
    """无 usage_logs → count=0, items=[]"""
    async def _run():
        await _reset_db()
        async with _get_client() as c:
            resp = await c.get("/dashboard/api/stats/per-user")
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["count"] == 0
            assert data["items"] == []
            assert data["days"] == 7

    asyncio.run(_run())


# ============================================================
# Test 2: per-user 有数据, 聚合 + NULL caller
# ============================================================

def test_per_user_usage_with_data():
    """2 个 caller 的 usage_logs → 聚合正确, NULL caller 归 "(未鉴权/legacy)" """
    async def _run():
        await _reset_db()
        from db.engine import async_session_factory

        uid_a = await _seed_user(async_session_factory, "alice", "Alice")
        uid_b = await _seed_user(async_session_factory, "bob", "Bob")

        # alice: 2 条
        await _seed_usage_log(async_session_factory, uid_a,
                              input_tokens=100, output_tokens=50, cost=0.01)
        await _seed_usage_log(async_session_factory, uid_a,
                              input_tokens=200, output_tokens=100, cost=0.02)
        # bob: 1 条
        await _seed_usage_log(async_session_factory, uid_b,
                              input_tokens=500, output_tokens=500, cost=0.05)
        # NULL caller (legacy/未鉴权): 1 条
        await _seed_usage_log(async_session_factory, None,
                              input_tokens=10, output_tokens=5, cost=0.001)

        async with _get_client() as c:
            resp = await c.get("/dashboard/api/stats/per-user")
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["count"] == 3, f"应有 3 行 (alice/bob/null), 实际 {data['count']}"

            # 按 token 总量降序: bob(1000) > alice(450) > null(15)
            items = data["items"]
            assert items[0]["username"] == "bob"
            assert items[0]["total_tokens"] == 1000
            assert items[0]["request_count"] == 1
            assert items[0]["total_cost"] == 0.05

            assert items[1]["username"] == "alice"
            assert items[1]["total_tokens"] == 450  # (100+50) + (200+100)
            assert items[1]["request_count"] == 2
            assert items[1]["total_cost"] == 0.03

            # NULL caller → "(未鉴权/legacy)"
            assert items[2]["caller_id"] is None
            assert items[2]["username"] == "(未鉴权/legacy)"
            assert items[2]["total_tokens"] == 15

    asyncio.run(_run())


# ============================================================
# Test 3: per-user days 过滤
# ============================================================

def test_per_user_usage_days_filter():
    """days=1 只看今天, days=30 看近 30 天"""
    async def _run():
        await _reset_db()
        from db.engine import async_session_factory

        uid = await _seed_user(async_session_factory, "alice", "Alice")

        # 一条最近的 (今天)
        await _seed_usage_log(async_session_factory, uid,
                              input_tokens=100, output_tokens=50,
                              created_at=datetime.now())
        # 一条 10 天前的
        await _seed_usage_log(async_session_factory, uid,
                              input_tokens=1000, output_tokens=1000,
                              created_at=datetime.now() - timedelta(days=10))

        async with _get_client() as c:
            # days=1: 只看到今天那条
            r1 = await c.get("/dashboard/api/stats/per-user", params={"days": 1})
            assert r1.status_code == 200
            d1 = r1.json()
            assert d1["count"] == 1
            assert d1["items"][0]["total_tokens"] == 150
            assert d1["items"][0]["request_count"] == 1

            # days=30: 两条都看到
            r2 = await c.get("/dashboard/api/stats/per-user", params={"days": 30})
            assert r2.status_code == 200
            d2 = r2.json()
            assert d2["count"] == 1  # 同一个 user, 聚合成 1 行
            assert d2["items"][0]["request_count"] == 2
            assert d2["items"][0]["total_tokens"] == 2150  # 150 + 2000

    asyncio.run(_run())


# ============================================================
# Test 4: list keys, 结构 + key_hash 不出现
# ============================================================

def test_list_keys():
    """2 user + 3 key → 结构正确, key_hash 永不返回"""
    async def _run():
        await _reset_db()
        from db.engine import async_session_factory

        uid_a = await _seed_user(async_session_factory, "alice", "Alice",
                                 role="admin", daily_limit=10000)
        uid_b = await _seed_user(async_session_factory, "bob", "Bob")

        kid_a1 = await _seed_api_key(async_session_factory, uid_a,
                                     key_plain="aimc-aaaa1111", is_admin=1,
                                     label="工作机")
        kid_a2 = await _seed_api_key(async_session_factory, uid_a,
                                     key_plain="aimc-aaaa2222", is_admin=0,
                                     status="revoked", label="旧key")
        kid_b1 = await _seed_api_key(async_session_factory, uid_b,
                                     key_plain="aimc-bbbb3333")

        async with _get_client() as c:
            resp = await c.get("/dashboard/api/keys")
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["count"] == 2

            users = data["users"]
            # 按 user.id 排序: alice 在前
            assert users[0]["username"] == "alice"
            assert users[0]["role"] == "admin"
            assert users[0]["daily_token_limit"] == 10000
            assert len(users[0]["keys"]) == 2

            # alice 的第一个 key: admin + active + 有 label
            k1 = users[0]["keys"][0]
            assert k1["key_prefix"] == "aimc-aaa"  # 前 8 字符
            assert k1["is_admin"] is True
            assert k1["status"] == "active"
            assert k1["label"] == "工作机"
            assert "key_hash" not in k1, "key_hash 不应出现在响应里!"

            # alice 的第二个 key: revoked
            k2 = users[0]["keys"][1]
            assert k2["status"] == "revoked"

            # bob: 1 个 key
            assert users[1]["username"] == "bob"
            assert len(users[1]["keys"]) == 1
            assert users[1]["keys"][0]["key_prefix"] == "aimc-bbb"
            assert users[1]["keys"][0]["is_admin"] is False

            # 整个响应文本里不应出现任何 SHA256 hash (64 位 hex)
            import json
            raw = json.dumps(data)
            import re
            # key_hash 是 64 字符 hex, 响应里不该有
            hash_matches = re.findall(r'[0-9a-f]{64}', raw)
            assert hash_matches == [], f"响应里发现疑似 key_hash: {hash_matches}"

    asyncio.run(_run())


# ============================================================
# Test 5: revoke key + 幂等
# ============================================================

def test_revoke_key():
    """revoke active key → status=revoked, 二次 revoke 幂等"""
    async def _run():
        await _reset_db()
        from db.engine import async_session_factory

        uid = await _seed_user(async_session_factory, "alice", "Alice")
        kid = await _seed_api_key(async_session_factory, uid,
                                  key_plain="aimc-revoke-me", status="active")

        async with _get_client() as c:
            # 第一次 revoke
            r1 = await c.post(f"/dashboard/api/keys/{kid}/revoke")
            assert r1.status_code == 200, r1.text
            d1 = r1.json()
            assert d1["status"] == "revoked"
            assert d1["key"]["status"] == "revoked"
            assert d1["key"]["id"] == kid

            # DB 验证
            from db.models import ApiKey
            async with async_session_factory() as session:
                k = await session.get(ApiKey, kid)
                assert k.status == "revoked"

            # 第二次 revoke (幂等, 不报错)
            r2 = await c.post(f"/dashboard/api/keys/{kid}/revoke")
            assert r2.status_code == 200, r2.text
            assert r2.json()["status"] == "revoked"

    asyncio.run(_run())


# ============================================================
# Test 6: revoke 不存在的 key → 404
# ============================================================

def test_revoke_nonexistent():
    """revoke 不存在的 id → 404"""
    async def _run():
        await _reset_db()
        async with _get_client() as c:
            resp = await c.post("/dashboard/api/keys/99999/revoke")
            assert resp.status_code == 404, resp.text
            assert "不存在" in resp.json()["detail"]

    asyncio.run(_run())
