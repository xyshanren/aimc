"""V6.0 Phase 2: 标价双轨成本审计测试

覆盖:
1. _estimate_list_cost (proxy): registry 标价 → 标价费用; 未知/NULL → None
2. 成本审计 4 端点 (dashboard): cost-by-model / cost-by-group / cost-by-user /
   cost-trend — 节省只统计标价已知行, 覆盖率正确
3. migration 2026_09_03_v60_phase2_cost_audit: 加列 + 标价回填 + 幂等

跑法:
    python -m pytest tests/test_v60_cost_audit.py -v
"""

import asyncio
import importlib.util
import os
import sqlite3
import sys
import tempfile
import types
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parent.parent.resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ============================================================
# Mock config + 临时 SQLite (同 test_v48_dashboard 模式)
# ============================================================

def _install_mock_env():
    tmp_dir = tempfile.mkdtemp(prefix="aimc_v60_cost_")
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
    )


_install_mock_env()


def _get_client():
    import httpx
    from fastapi import FastAPI
    from dashboard.routes import router as dashboard_router

    app = FastAPI()
    app.include_router(dashboard_router)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


async def _reset_db():
    """建表 + 清空 usage_logs (清表走 sqlite3 内联字面量, 不经 execute 变量)"""
    import config
    from db.init_db import init_db

    await init_db()
    conn = sqlite3.connect(config.settings.db_path)
    conn.execute("DELETE FROM usage_logs")
    conn.commit()
    conn.close()


async def _seed_usage_log(factory, model, group_id=None, caller_id=None,
                          input_tokens=1000, output_tokens=500,
                          cost=0.0, list_cost=None, created_at=None):
    from db.models import UsageLog

    async with factory() as session:
        log = UsageLog(
            model=model,
            group_id=group_id,
            caller_id=caller_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            status_code=200,
            cost_estimate=cost,
            list_cost_estimate=list_cost,
            created_at=created_at or datetime.now(),
        )
        session.add(log)
        await session.commit()
        return log.id


def _load_migration():
    """按路径加载迁移模块 (migrations 目录无 __init__)"""
    path = ROOT / "db" / "migrations" / "2026_09_03_v60_phase2_cost_audit.py"
    spec = importlib.util.spec_from_file_location("v60_cost_migration", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ============================================================
# Part 1: _estimate_list_cost 单元测试
# ============================================================

def test_estimate_list_cost_computes_from_registry():
    """registry 有标价 → (in*lin + out*lout)/1e6"""
    from gateway.proxy import _estimate_list_cost
    from gateway.router import route_engine

    original = route_engine._registry_cache
    route_engine._registry_cache = {
        "glm-5.3-flash": SimpleNamespace(list_input_price=0.8, list_output_price=2.8),
    }
    try:
        # 1M in + 0.1M out @ (0.8, 2.8) → 0.8 + 0.28 = 1.08 元
        cost = _estimate_list_cost("glm-5.3-flash", 1_000_000, 100_000)
        assert cost is not None
        assert abs(cost - 1.08) < 1e-9
    finally:
        route_engine._registry_cache = original


def test_estimate_list_cost_unknown_model_returns_none():
    from gateway.proxy import _estimate_list_cost
    from gateway.router import route_engine

    original = route_engine._registry_cache
    route_engine._registry_cache = {}
    try:
        assert _estimate_list_cost("no-such-model", 1000, 100) is None
    finally:
        route_engine._registry_cache = original


def test_estimate_list_cost_null_price_returns_none():
    """标价 NULL (未知) → None, 不编造 0"""
    from gateway.proxy import _estimate_list_cost
    from gateway.router import route_engine

    original = route_engine._registry_cache
    route_engine._registry_cache = {
        "kimi-k3": SimpleNamespace(list_input_price=None, list_output_price=None),
        "legacy-model": SimpleNamespace(),  # 旧缓存对象没有这两个属性
    }
    try:
        assert _estimate_list_cost("kimi-k3", 1000, 100) is None
        assert _estimate_list_cost("legacy-model", 1000, 100) is None
    finally:
        route_engine._registry_cache = original


def test_estimate_list_cost_zero_price_is_zero():
    """官方免费模型 (标价 0.0) → 0.0, 与 None (未知) 区分"""
    from gateway.proxy import _estimate_list_cost
    from gateway.router import route_engine

    original = route_engine._registry_cache
    route_engine._registry_cache = {
        "glm-4.7-flash": SimpleNamespace(list_input_price=0.0, list_output_price=0.0),
    }
    try:
        cost = _estimate_list_cost("glm-4.7-flash", 1_000_000, 1_000_000)
        assert cost == 0.0
    finally:
        route_engine._registry_cache = original


def test_write_call_sites_pass_list_cost():
    """防回归: 两个 _write_usage_log 调用点必须传 list_cost_estimate

    V6.0 Phase2 上产首日发现: 两处调用点算了值但没传参, 落库恒 NULL
    (非流式 + 流式共 2 个调用点 + UsageLog 构造, 共 3 处 kwarg)
    """
    src = (ROOT / "gateway" / "proxy.py").read_text(encoding="utf-8")
    n = src.count("list_cost_estimate=list_cost_estimate")
    assert n >= 3, (
        f"_write_usage_log 调用点/UsageLog 构造应有 >=3 处传 list_cost_estimate, 实际 {n}"
    )


# ============================================================
# Part 2: 成本审计 4 端点 (asyncio.run 包装, 同 test_v48_dashboard 惯例)
# ============================================================

def test_cost_by_model_savings_only_priced_rows():
    """节省 = 标价 - 实付, 只统计标价已知行; 标价 NULL 行不计入且覆盖率暴露"""
    from db.engine import async_session_factory

    async def _run():
        await _reset_db()
        # glm-5.3-flash: 免费 (实付 0), 标价已知 → 节省 = 标价
        await _seed_usage_log(async_session_factory, "glm-5.3-flash",
                              group_id="tier:strong", caller_id=1,
                              input_tokens=1000, output_tokens=500,
                              cost=0.0, list_cost=0.0022)
        # deepseek-v4-flash: 付费 (实付 0.5), 标价 0.9 → 节省 0.4
        await _seed_usage_log(async_session_factory, "deepseek-v4-flash",
                              group_id="tier:strong", caller_id=1,
                              input_tokens=1000, output_tokens=500,
                              cost=0.5, list_cost=0.9)
        # kimi-k3: 标价未知 → 不计入节省, known=0
        await _seed_usage_log(async_session_factory, "kimi-k3",
                              group_id="tier:balanced", caller_id=2,
                              input_tokens=1000, output_tokens=500,
                              cost=0.0, list_cost=None)

        async with _get_client() as client:
            resp = await client.get("/dashboard/api/usage/cost-by-model?days=7")
        assert resp.status_code == 200
        return resp.json()

    data = asyncio.run(_run())

    assert data["days"] == 7
    items = {i["model"]: i for i in data["items"]}
    assert set(items) == {"glm-5.3-flash", "deepseek-v4-flash", "kimi-k3"}

    glm = items["glm-5.3-flash"]
    assert glm["cost_yuan"] == 0.0
    assert abs(glm["list_cost_yuan"] - 0.0022) < 1e-6
    assert abs(glm["saved_yuan"] - 0.0022) < 1e-6
    assert glm["list_known_requests"] == 1

    ds = items["deepseek-v4-flash"]
    assert abs(ds["saved_yuan"] - 0.4) < 1e-6  # 0.9 - 0.5

    kimi = items["kimi-k3"]
    assert kimi["list_cost_yuan"] == 0.0
    assert kimi["saved_yuan"] == 0.0
    assert kimi["list_known_requests"] == 0

    totals = data["totals"]
    assert totals["request_count"] == 3
    assert totals["list_known_requests"] == 2
    assert abs(totals["list_coverage_pct"] - 66.7) < 0.1
    # 总节省 = (0.0022 + 0.9) - (0 + 0.5) = 0.4022; kimi 不计入
    assert abs(totals["saved_yuan"] - 0.4022) < 1e-4


def test_cost_by_group_and_user():
    """按组/按用户聚合字段齐全, NULL group_id 归 '(精确路由)'"""
    from db.engine import async_session_factory

    async def _run():
        await _reset_db()
        await _seed_usage_log(async_session_factory, "glm-5.3-flash",
                              group_id="tier:strong", caller_id=None,
                              cost=0.0, list_cost=0.01)
        await _seed_usage_log(async_session_factory, "deepseek-v4-flash",
                              group_id=None, caller_id=7,
                              cost=0.2, list_cost=0.3)

        async with _get_client() as client:
            resp_group = await client.get("/dashboard/api/usage/cost-by-group?days=7")
            resp_user = await client.get("/dashboard/api/usage/cost-by-user?days=7")
        return resp_group, resp_user

    resp_group, resp_user = asyncio.run(_run())

    assert resp_group.status_code == 200
    groups = {i["group_id"]: i for i in resp_group.json()["items"]}
    assert "tier:strong" in groups
    assert "(精确路由)" in groups
    assert abs(groups["tier:strong"]["saved_yuan"] - 0.01) < 1e-6

    assert resp_user.status_code == 200
    users = {i["username"]: i for i in resp_user.json()["items"]}
    assert "(未鉴权/legacy)" in users
    assert users["(未鉴权/legacy)"]["list_known_requests"] == 1


def test_cost_trend_ordering_and_days_filter():
    """trend 按时间升序; days 窗口过滤生效"""
    from db.engine import async_session_factory

    async def _run():
        await _reset_db()
        now = datetime.now()
        await _seed_usage_log(async_session_factory, "glm-5.3-flash",
                              cost=0.0, list_cost=0.01, created_at=now - timedelta(days=10))
        await _seed_usage_log(async_session_factory, "glm-5.3-flash",
                              cost=0.0, list_cost=0.02, created_at=now - timedelta(days=1))
        await _seed_usage_log(async_session_factory, "glm-5.3-flash",
                              cost=0.0, list_cost=0.04, created_at=now)

        async with _get_client() as client:
            resp = await client.get("/dashboard/api/usage/cost-trend?days=7")
        assert resp.status_code == 200
        return resp.json()

    data = asyncio.run(_run())
    dates = [i["date"] for i in data["items"]]
    assert dates == sorted(dates), "trend 应时间升序"
    # 10 天前那条被 days=7 过滤
    assert sum(i["request_count"] for i in data["items"]) == 2
    assert abs(data["totals"]["list_cost_yuan"] - 0.06) < 1e-6


def test_cost_endpoints_empty_db():
    """空库 → 200 + 零值, 不 500"""

    async def _run():
        await _reset_db()
        async with _get_client() as client:
            out = []
            for ep in ("cost-by-model", "cost-by-group", "cost-by-user", "cost-trend"):
                resp = await client.get(f"/dashboard/api/usage/{ep}?days=7")
                out.append((ep, resp.status_code, resp.json()))
        return out

    for ep, status, data in asyncio.run(_run()):
        assert status == 200, ep
        assert data["items"] == []
        assert data["totals"]["request_count"] == 0
        assert data["totals"]["list_coverage_pct"] == 0.0


# ============================================================
# Part 3: migration 幂等 + 回填
# ============================================================

def _make_old_schema_db(db_path):
    """旧 schema (无标价列) 的最小库 (usage_logs 带成本审计所需列)"""
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE model_registry (id INTEGER PRIMARY KEY, model_id TEXT)")
    conn.execute(
        "CREATE TABLE usage_logs ("
        "id INTEGER PRIMARY KEY, model TEXT, "
        "input_tokens INTEGER, output_tokens INTEGER)"
    )
    conn.execute("INSERT INTO model_registry (model_id) VALUES ('glm-5.3-flash')")
    conn.execute("INSERT INTO model_registry (model_id) VALUES ('kimi-k3')")
    conn.execute("INSERT INTO model_registry (model_id) VALUES ('minimax-m3')")
    # 存量 usage 行: glm-5.3-flash 1M in / 0.5M out → 标价 0.8 + 1.4 = 2.2 元;
    # kimi-k3 标价未知 → 保持 NULL
    conn.execute(
        "INSERT INTO usage_logs (model, input_tokens, output_tokens) "
        "VALUES ('glm-5.3-flash', 1000000, 500000)"
    )
    conn.execute(
        "INSERT INTO usage_logs (model, input_tokens, output_tokens) "
        "VALUES ('kimi-k3', 1000, 500)"
    )
    conn.commit()
    conn.close()


def test_migration_adds_columns_and_backfills(tmp_path):
    migration = _load_migration()

    db_path = str(tmp_path / "mig.db")
    _make_old_schema_db(db_path)
    sys.modules["config"].settings.db_path = db_path

    # dry-run 不改库
    assert migration.run(apply=False) == 0
    conn = sqlite3.connect(db_path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(model_registry)")}
    assert "list_input_price" not in cols
    conn.close()

    # apply: 加列 + 回填
    assert migration.run(apply=True) == 0
    conn = sqlite3.connect(db_path)
    reg_cols = {r[1] for r in conn.execute("PRAGMA table_info(model_registry)")}
    usage_cols = {r[1] for r in conn.execute("PRAGMA table_info(usage_logs)")}
    assert {"list_input_price", "list_output_price"} <= reg_cols
    assert "list_cost_estimate" in usage_cols

    rows = {
        r[0]: (r[1], r[2])
        for r in conn.execute(
            "SELECT model_id, list_input_price, list_output_price FROM model_registry"
        )
    }
    assert rows["glm-5.3-flash"] == (0.8, 2.8)   # 原价标价
    assert rows["kimi-k3"] == (None, None)        # 未知不编造
    assert rows["minimax-m3"] == (None, None)     # 未知不编造

    # 步骤 4: 存量 usage_logs 标价回填
    usage_rows = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT model, list_cost_estimate FROM usage_logs"
        )
    }
    assert abs(usage_rows["glm-5.3-flash"] - 2.2) < 1e-9  # 1M*0.8 + 0.5M*2.8 / 1e6
    assert usage_rows["kimi-k3"] is None                   # 标价未知 → NULL
    conn.close()

    # 再跑一遍: 幂等, 已有标价不被覆盖
    assert migration.run(apply=True) == 0
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE model_registry SET list_input_price = 9.99 WHERE model_id = 'glm-5.3-flash'"
    )
    conn.commit()
    conn.close()
    assert migration.run(apply=True) == 0
    conn = sqlite3.connect(db_path)
    v = conn.execute(
        "SELECT list_input_price FROM model_registry WHERE model_id = 'glm-5.3-flash'"
    ).fetchone()[0]
    assert v == 9.99, "人工修正不应被迁移覆盖"
    conn.close()


def test_migration_missing_models_skipped(tmp_path):
    """LIST_PRICES 里有但 registry 没有的模型 → 跳过不报错"""
    migration = _load_migration()

    db_path = str(tmp_path / "mig2.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE model_registry (id INTEGER PRIMARY KEY, model_id TEXT)")
    conn.execute("CREATE TABLE usage_logs (id INTEGER PRIMARY KEY)")
    # 只放一个 LIST_PRICES 之外的模型
    conn.execute("INSERT INTO model_registry (model_id) VALUES ('totally-unknown')")
    conn.commit()
    conn.close()
    sys.modules["config"].settings.db_path = db_path

    assert migration.run(apply=True) == 0
    conn = sqlite3.connect(db_path)
    n = conn.execute(
        "SELECT COUNT(*) FROM model_registry WHERE list_input_price IS NOT NULL"
    ).fetchone()[0]
    assert n == 0
    conn.close()
