"""V6.1 架构友好化测试: 鉴权统一 + POST /api/keys + 统一 CLI

覆盖 (docs/next-major-v6-plan.md §3.4 步骤 1/2/3):
1. 鉴权统一: dashboard API 接受 admin api_key (aimc- 前缀 + is_admin=1),
   普通用户 key / 错误凭据仍 401
2. POST /dashboard/api/keys: 创建/复用 user + 发 key (明文只回一次),
   与 add_user_key.py 语义对齐
3. scripts/aimc.py 统一 CLI: 参数树 / SSRF 目标白名单 / REST 映射路径

跑法:
    python -m pytest tests/test_v61_arch.py -v
"""

import asyncio
import hashlib
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent.resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DASH_TOKEN = "dash-token-abc123"
ADMIN_KEY = "aimc-adminkey0000000000000000000000000"
USER_KEY = "aimc-userkey00000000000000000000000000"


def _install_mock_env():
    tmp_dir = tempfile.mkdtemp(prefix="aimc_v61_arch_")
    db_path = os.path.join(tmp_dir, "test.db")
    db_url = "sqlite+aiosqlite:///" + db_path

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
        # V6.1 鉴权统一测试需要
        dashboard_token=DASH_TOKEN,
        dashboard_auth_enabled=True,
    )


_install_mock_env()


def _get_client():
    """挂 dashboard router + RequestLoggingMiddleware (鉴权在 middleware 里)"""
    import httpx
    from fastapi import FastAPI
    from dashboard.routes import router as dashboard_router
    from gateway.middleware import RequestLoggingMiddleware

    app = FastAPI()
    app.add_middleware(RequestLoggingMiddleware)
    app.include_router(dashboard_router)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


async def _reset_and_seed():
    import config
    from db.engine import async_session_factory
    from db.init_db import init_db
    from db.models import ApiKey, User
    from sqlalchemy import select

    await init_db()
    # 清 middleware key 缓存 (30s TTL 会串测试)
    import gateway.middleware as mw
    mw._apikey_cache.clear()

    # 清库走 ORM 逐对象删除 (select 调用点内联, 规避字符串 DELETE)
    async with async_session_factory() as session:
        for k in (await session.execute(select(ApiKey))).scalars().all():
            await session.delete(k)
        for u in (await session.execute(select(User))).scalars().all():
            await session.delete(u)
        await session.commit()

    async with async_session_factory() as session:
        for username, key_plain, is_admin in (
            ("admin_user", ADMIN_KEY, 1),
            ("normal_user", USER_KEY, 0),
        ):
            u = User(username=username, display_name=username, role="user", status="active")
            session.add(u)
            await session.flush()
            session.add(ApiKey(
                user_id=u.id,
                key_hash=hashlib.sha256(key_plain.encode()).hexdigest(),
                key_prefix=key_plain[:8],
                is_admin=is_admin,
                status="active",
            ))
        await session.commit()


# ============================================================
# Part 1: 鉴权统一 — admin api_key 等价 dashboard token
# ============================================================

def test_admin_api_key_accesses_dashboard_api():
    async def _run():
        await _reset_and_seed()
        async with _get_client() as client:
            resp = await client.get(
                "/dashboard/api/usage/summary?days=7",
                headers={"Authorization": "Bearer " + ADMIN_KEY},
            )
        return resp

    resp = asyncio.run(_run())
    assert resp.status_code == 200, f"admin api_key 应放行, 实际 {resp.status_code}: {resp.text[:200]}"


def test_non_admin_api_key_still_401():
    async def _run():
        await _reset_and_seed()
        async with _get_client() as client:
            resp = await client.get(
                "/dashboard/api/usage/summary?days=7",
                headers={"Authorization": "Bearer " + USER_KEY},
            )
        return resp

    resp = asyncio.run(_run())
    assert resp.status_code == 401, "普通用户 key 不应访问 dashboard API"


def test_dashboard_token_still_works_and_wrong_rejected():
    async def _run():
        await _reset_and_seed()
        async with _get_client() as client:
            ok = await client.get(
                "/dashboard/api/usage/summary?days=7",
                headers={"Authorization": "Bearer " + DASH_TOKEN},
            )
            bad = await client.get(
                "/dashboard/api/usage/summary?days=7",
                headers={"Authorization": "Bearer wrong-token"},
            )
        return ok, bad

    ok, bad = asyncio.run(_run())
    assert ok.status_code == 200, "dashboard token 原路径不应被破坏"
    assert bad.status_code == 401


# ============================================================
# Part 2: POST /dashboard/api/keys (等价 add_user_key.py)
# ============================================================

def test_create_key_new_user_and_reuse():
    async def _run():
        await _reset_and_seed()
        auth = {"Authorization": "Bearer " + DASH_TOKEN}
        async with _get_client() as client:
            r1 = await client.post("/dashboard/api/keys", headers=auth, json={
                "username": "zhangsan", "display_name": "张三",
                "label": "工作机", "admin": True,
                "daily_limit": 100000,
            })
            r2 = await client.post("/dashboard/api/keys", headers=auth, json={
                "username": "zhangsan", "label": "笔记本",
            })
            lst = await client.get("/dashboard/api/keys", headers=auth)
        return r1, r2, lst

    r1, r2, lst = asyncio.run(_run())
    assert r1.status_code == 201, r1.text
    body1 = r1.json()
    assert body1["key_plain"].startswith("aimc-")
    assert body1["key_prefix"] == body1["key_plain"][:8]
    assert body1["label"] == "工作机"

    # 同 username 第二次: 复用 user, 只加 key
    assert r2.status_code == 201
    body2 = r2.json()
    assert body2["user_id"] == body1["user_id"]
    assert body2["key_id"] != body1["key_id"]

    # 列表里该 user 有 2 把 key
    users = {u["username"]: u for u in lst.json()["users"]}
    assert len(users["zhangsan"]["keys"]) == 2
    assert users["zhangsan"]["daily_token_limit"] == 100000
    # key_hash 永不出现
    assert "key_hash" not in str(lst.json())


def test_create_key_requires_username():
    async def _run():
        await _reset_and_seed()
        async with _get_client() as client:
            return await client.post(
                "/dashboard/api/keys",
                headers={"Authorization": "Bearer " + DASH_TOKEN},
                json={},
            )

    resp = asyncio.run(_run())
    assert resp.status_code == 400


# ============================================================
# Part 3: 统一 CLI (scripts/aimc.py)
# ============================================================

def test_cli_help_exits_zero():
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "aimc.py"), "--help"],
        capture_output=True, text=True,
    )
    assert r.returncode == 0
    assert "accounts" in r.stdout and "keys" in r.stdout and "usage" in r.stdout


def test_cli_usage_command_mapping():
    from scripts.aimc import build_parser, cmd_usage

    args = build_parser().parse_args(["usage", "cost-by-model", "--days", "30"])
    assert args.func is cmd_usage
    assert args.metric == "cost-by-model"
    assert args.days == 30


def test_cli_ssrf_guard():
    from scripts.aimc import _assert_safe_target

    # 合法: 回环 / 内网 / localhost
    _assert_safe_target("http://127.0.0.1:8080/health")
    _assert_safe_target("http://192.168.56.12:8080/health")
    _assert_safe_target("http://localhost:8080/health")
    # 非法: 公网 / 非 http / 非 IP 主机名
    with pytest.raises(SystemExit):
        _assert_safe_target("http://8.8.8.8/health")
    with pytest.raises(SystemExit):
        _assert_safe_target("https://example.com/api")
    with pytest.raises(SystemExit):
        _assert_safe_target("ftp://127.0.0.1/x")
