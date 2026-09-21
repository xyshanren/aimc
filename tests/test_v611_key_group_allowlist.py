"""V6.11: key 级组白名单 (api_keys.allowed_groups) 测试

覆盖 (2026-09-22 拍板方案):
1. 门禁函数 check_key_group_allowed: 组命中/未命中, 裸模型成员归属,
   无组成员默认拒绝, NULL/空=不限
2. middleware 解析 allowed_groups: JSON → list, NULL → None, 坏 JSON → None
3. chat_completions 403 集成: 受限 key 请求白名单外组 → 403 key_group_not_allowed
   (门禁在路由前, 不触达上游)
4. POST /dashboard/api/keys: allowed_groups 校验 (未知组 400 / 逗号串归一 /
   空数组归一为不限) + GET 列表回显

跑法:
    python -m pytest tests/test_v611_key_group_allowlist.py -v
"""

import asyncio
import hashlib
import json
import os
import sys
import tempfile
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent.resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DASH_TOKEN = "dash-token-v611-abc"
ADMIN_KEY = "aimc-v611adminkey000000000000000000000"
USER_KEY = "aimc-v611userkey0000000000000000000000"
# legacy 单 key (middleware 兜底路径) — 拼接构造避免测试文件里出现完整凭据字面量
LEGACY_KEY = "legacy-" + "single-" + "key"
NULLGROUP_KEY = "aimc-nullgroups" + "key000000000000000"
UNRESTRICTED_KEY = "aimc-unrestricted" + "key000000000000000"
BADJSON_KEY = "aimc-badjson" + "key000000000000000000"


def _install_mock_env():
    tmp_dir = tempfile.mkdtemp(prefix="aimc_v611_")
    db_path = os.path.join(tmp_dir, "test.db")
    db_url = "sqlite+aiosqlite:///" + db_path

    sys.modules.setdefault("config", types.ModuleType("config"))
    _settings = types.SimpleNamespace(
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
        dashboard_token=DASH_TOKEN,
        dashboard_auth_enabled=True,
        auth_enabled=True,
        max_fallback_retries=0,
    )
    _settings.api_key = LEGACY_KEY
    sys.modules["config"].settings = _settings


_install_mock_env()


# ============================================================
# Helper: 临时库 + seed (users / api_keys / model_groups)
# ============================================================

def _fresh_factory():
    """每个测试独立临时库 (只 patch db.engine.async_session_factory, 不回改
    config.settings — 其他导入链可能带回真 pydantic Settings 且不可写)"""
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
    import db.engine as dbeng
    from db.models import Base

    tmp_dir = tempfile.mkdtemp(prefix="aimc_v611_db_")
    db_url = "sqlite+aiosqlite:///" + os.path.join(tmp_dir, "test.db")

    eng = create_async_engine(db_url, echo=False, future=True)
    factory = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)

    async def _init():
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_init())
    loop.close()

    dbeng.async_session_factory = factory
    return factory


async def _seed_groups(factory):
    """建两组: tier:flagship / tier:light"""
    from db.models import ModelGroup
    async with factory() as s:
        for gid, sort_order in (("tier:flagship", 10), ("tier:light", 40)):
            s.add(ModelGroup(group_id=gid, display_name=gid, sort_order=sort_order))
        await s.commit()


async def _seed_key(factory, username, key_plain, is_admin=0, allowed_groups=None,
                    allowed_groups_raw=None):
    """建 user + api_key. allowed_groups 传 list 自动 json 化;
    allowed_groups_raw 直接落库 (坏 JSON 场景用), 优先级更高"""
    from db.models import User, ApiKey
    async with factory() as s:
        u = User(username=username, display_name=username, role="user", status="active")
        s.add(u)
        await s.flush()
        s.add(ApiKey(
            user_id=u.id,
            key_hash=hashlib.sha256(key_plain.encode()).hexdigest(),
            key_prefix=key_plain[:8],
            is_admin=is_admin,
            status="active",
            allowed_groups=(
                allowed_groups_raw if allowed_groups_raw is not None
                else (json.dumps(allowed_groups) if allowed_groups else None)
            ),
        ))
        await s.commit()


def _clear_key_cache():
    import gateway.middleware as mw
    mw._apikey_cache.clear()


# ============================================================
# Part 1: 门禁函数单测 (stub engine)
# ============================================================

class _StubEngine:
    """仿 RouteEngine 组缓存 (生产形态: tier:flagship 多成员, tier:light 单成员)"""

    def __init__(self):
        self._groups_cache = {"tier:flagship": object(), "tier:light": object()}
        self._group_members_cache = {
            "tier:flagship": ["glm-5.2", "deepseek-v4-pro", "minimax-m3"],
            "tier:light": ["deepseek-flash"],
        }

    def is_known_group(self, name):
        return name in self._groups_cache

    def groups_containing_model(self, model):
        return [g for g, m in self._group_members_cache.items() if model in m]


def test_gate_none_means_unrestricted():
    from gateway.routes import check_key_group_allowed
    eng = _StubEngine()
    assert check_key_group_allowed("tier:flagship", None, eng) is None
    assert check_key_group_allowed("tier:flagship", [], eng) is None
    assert check_key_group_allowed("whatever-model", None, eng) is None


def test_gate_group_hit_and_miss():
    from gateway.routes import check_key_group_allowed
    eng = _StubEngine()
    # 白名单含 flagship → 放行; 只含 light → 拒 flagship 且原因带合法组
    assert check_key_group_allowed("tier:flagship", ["tier:flagship", "tier:light"], eng) is None
    reason = check_key_group_allowed("tier:flagship", ["tier:light"], eng)
    assert reason is not None and "tier:flagship" in reason and "tier:light" in reason


def test_gate_raw_model_membership():
    from gateway.routes import check_key_group_allowed
    eng = _StubEngine()
    # deepseek-flash ∈ tier:light → 白名单含 light 即放行
    assert check_key_group_allowed("deepseek-flash", ["tier:light"], eng) is None
    # glm-5.2 只 ∈ flagship → 拒
    assert check_key_group_allowed("glm-5.2", ["tier:light"], eng) is not None
    # glm-5.2 同属 flagship+light 双组场景 → 任一允许即放行
    eng2 = _StubEngine()
    eng2._group_members_cache["tier:light"].append("glm-5.2")
    assert check_key_group_allowed("glm-5.2", ["tier:light"], eng2) is None


def test_gate_no_membership_model_default_deny():
    from gateway.routes import check_key_group_allowed
    eng = _StubEngine()
    reason = check_key_group_allowed("some-unlisted-model", ["tier:light"], eng)
    assert reason is not None and "无组成员" in reason


def test_gate_unknown_group_name_treated_as_raw_model():
    """未知组名 (不在 model_groups) 按裸模型走成员归属检查, 不按组名检查"""
    from gateway.routes import check_key_group_allowed
    eng = _StubEngine()
    reason = check_key_group_allowed("scene:code", ["tier:light"], eng)
    assert reason is not None and "无组成员" in reason


# ============================================================
# Part 2: middleware 解析 allowed_groups
# ============================================================

def test_middleware_passes_allowed_groups():
    factory = _fresh_factory()
    _clear_key_cache()

    async def _run():
        from gateway.middleware import _lookup_api_key
        await _seed_groups(factory)
        await _seed_key(factory, "alice", USER_KEY, allowed_groups=["tier:light", "tier:balanced"])
        info = await _lookup_api_key(USER_KEY)
        assert info is not None
        assert info.allowed_groups == ["tier:light", "tier:balanced"]

    asyncio.run(_run())


def test_middleware_null_and_bad_json_means_unrestricted():
    factory = _fresh_factory()
    _clear_key_cache()

    async def _run():
        from gateway.middleware import _lookup_api_key
        # NULL → 不限
        await _seed_key(factory, "bob", NULLGROUP_KEY, allowed_groups=None)
        info = await _lookup_api_key(NULLGROUP_KEY)
        assert info is not None and info.allowed_groups is None
        # 坏 JSON → 按不限 (拒绝所有请求比放行更伤, 写入口已校验, 坏值只可能是手改库)
        await _seed_key(factory, "eve", BADJSON_KEY, allowed_groups_raw="{bad json")
        info2 = await _lookup_api_key(BADJSON_KEY)
        assert info2 is not None and info2.allowed_groups is None

    asyncio.run(_run())


# ============================================================
# Part 3: chat_completions 403 集成 (门禁在路由选择前, 不触达上游)
# ============================================================

def _build_gateway_app():
    import httpx
    from fastapi import FastAPI
    from gateway.routes import router as gateway_router
    from gateway.middleware import RequestLoggingMiddleware

    app = FastAPI()
    app.add_middleware(RequestLoggingMiddleware)
    app.include_router(gateway_router)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


def test_chat_completions_group_not_allowed_403():
    factory = _fresh_factory()
    _clear_key_cache()

    async def _run():
        await _seed_groups(factory)
        await _seed_key(factory, "alice", USER_KEY, allowed_groups=["tier:light"])
        async with _build_gateway_app() as client:
            body = {"model": "tier:flagship", "messages": [{"role": "user", "content": "hi"}]}
            return await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer " + USER_KEY},
                json=body,
            )

    resp = asyncio.run(_run())
    assert resp.status_code == 403, (
        f"受限 key 打白名单外组应 403, 实际 {resp.status_code}: {resp.text[:200]}"
    )
    err = resp.json()["error"]
    assert err["code"] == "key_group_not_allowed"
    assert "tier:flagship" in err["message"]


def test_chat_completions_unrestricted_key_passes_gate():
    """allowed_groups=NULL 的 key 不被门禁拦 (放行后到路由层, 空缓存 → 非 403 终态)"""
    factory = _fresh_factory()
    _clear_key_cache()

    async def _run():
        await _seed_groups(factory)
        await _seed_key(factory, "carol", UNRESTRICTED_KEY, allowed_groups=None)
        async with _build_gateway_app() as client:
            body = {"model": "tier:flagship", "messages": [{"role": "user", "content": "hi"}]}
            return await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer " + UNRESTRICTED_KEY},
                json=body,
            )

    resp = asyncio.run(_run())
    assert resp.status_code != 403, f"不限 key 不应被门禁拦: {resp.text[:200]}"


# ============================================================
# Part 4: POST /dashboard/api/keys allowed_groups 校验 + 回显
# ============================================================

def _get_dashboard_client():
    import httpx
    from fastapi import FastAPI
    from dashboard.routes import router as dashboard_router
    from gateway.middleware import RequestLoggingMiddleware

    app = FastAPI()
    app.add_middleware(RequestLoggingMiddleware)
    app.include_router(dashboard_router)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


def test_create_key_with_allowed_groups():
    factory = _fresh_factory()
    _clear_key_cache()

    async def _run():
        from db.models import User, ApiKey, ModelGroup
        # seed admin (发 key 用) + 两组
        async with factory() as s:
            u = User(username="boss", display_name="boss", role="admin", status="active")
            s.add(u)
            await s.flush()
            s.add(ApiKey(
                user_id=u.id,
                key_hash=hashlib.sha256(ADMIN_KEY.encode()).hexdigest(),
                key_prefix=ADMIN_KEY[:8], is_admin=1, status="active",
            ))
            for gid, sort_order in (("tier:flagship", 10), ("tier:light", 40)):
                s.add(ModelGroup(group_id=gid, display_name=gid, sort_order=sort_order))
            await s.commit()

        auth = {"Authorization": "Bearer " + DASH_TOKEN}
        async with _get_dashboard_client() as client:
            ok = await client.post("/dashboard/api/keys", headers=auth, json={
                "username": "xiaowang", "label": "外发机",
                "allowed_groups": ["tier:light"],
            })
            comma = await client.post("/dashboard/api/keys", headers=auth, json={
                "username": "xiaoli", "allowed_groups": "tier:light,tier:flagship",
            })
            empty = await client.post("/dashboard/api/keys", headers=auth, json={
                "username": "xiaozhao", "allowed_groups": [],
            })
            bad = await client.post("/dashboard/api/keys", headers=auth, json={
                "username": "xiaoqian", "allowed_groups": ["tier:balance"],
            })
            lst = await client.get("/dashboard/api/keys", headers=auth)
        return ok, comma, empty, bad, lst

    ok, comma, empty, bad, lst = asyncio.run(_run())
    assert ok.status_code == 201, ok.text
    assert ok.json()["allowed_groups"] == ["tier:light"]
    assert comma.status_code == 201 and comma.json()["allowed_groups"] == ["tier:light", "tier:flagship"]
    assert empty.status_code == 201 and empty.json()["allowed_groups"] is None
    assert bad.status_code == 400, "typo 组名应 400"
    assert "tier:balance" in bad.json()["detail"]

    keys = []
    for u in lst.json()["users"]:
        keys.extend(u["keys"])
    lw = [k for k in keys if k.get("label") == "外发机"]
    assert lw and lw[0]["allowed_groups"] == ["tier:light"]
    unrestricted = [k for k in keys if k.get("allowed_groups") is None]
    assert unrestricted, "NULL 白名单 key 应回显 None"


def test_add_user_key_script_arg_parsing():
    """--allowed-groups 参数解析 (不发真请求, 只验证 parser 语义)"""
    from scripts.add_user_key import build_parser
    p = build_parser()
    args = p.parse_args(["--username", "u1", "--allowed-groups", "tier:light, tier:balanced"])
    assert args.allowed_groups == "tier:light, tier:balanced"
    args2 = p.parse_args(["--username", "u1"])
    assert args2.allowed_groups is None
