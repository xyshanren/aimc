# -*- coding: utf-8 -*-
"""V6.12 用量页用户/Key 维度端点测试

覆盖:
1. /api/usage/by-user: 多用户聚合 + totals + key 下钻 (含 legacy 行) + label 消歧
2. /api/usage/perf dim=user/key: caller/key 维聚合 + 4xx 过滤 + display_name 优先
3. /api/usage/user-trend: 按日分桶补零 + dim=user/key + top-8 截断 (其他桶) + days=1 小时桶
4. /api/requests caller_id 筛选 (复合 status 过滤)

跑法: python -m pytest tests/test_v612_usage_by_user.py -v
"""

import os
import sys
import tempfile
import types
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).parent.parent.resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _install_mock_env():
    tmp_dir = tempfile.mkdtemp(prefix="aimc_v612_by_user_")
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
    import httpx
    from fastapi import FastAPI
    from dashboard.routes import router as dashboard_router

    app = FastAPI()
    app.include_router(dashboard_router)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


async def _reset_db(factory):
    """drop_all + create_all 重建 (与 async_session_factory 同文件库)"""
    from sqlalchemy.ext.asyncio import create_async_engine
    from db.models import Base
    from config import settings

    eng = create_async_engine(f"sqlite+aiosqlite:///{settings.db_path}")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await eng.dispose()


async def _seed_users_and_keys(factory):
    """alice(id=1, Alice) + bob(id=2, Bob); key11/12 属 alice, key13 属 bob。

    key11 与 key13 同 label "dsh" → 消歧场景。
    """
    from db.models import User, ApiKey

    async with factory() as s:
        s.add_all([
            User(id=1, username="alice", display_name="Alice", role="user", status="active"),
            User(id=2, username="bob", display_name="Bob", role="user", status="active"),
            ApiKey(id=11, user_id=1, key_hash="h11", key_prefix="aimc-11", is_admin=0, status="active", label="dsh"),
            ApiKey(id=12, user_id=1, key_hash="h12", key_prefix="aimc-12", is_admin=0, status="active", label="zcode"),
            ApiKey(id=13, user_id=2, key_hash="h13", key_prefix="aimc-13", is_admin=0, status="active", label="dsh"),
        ])
        await s.commit()


def _log(caller_id, key_id, in_tok, out_tok, hours_ago=1, status=200, lat=100):
    from db.models import UsageLog

    return UsageLog(
        caller_id=caller_id, key_id=key_id,
        model="glm-5.2", input_tokens=in_tok, output_tokens=out_tok,
        reasoning_tokens=None, latency_ms=lat, status_code=status,
        created_at=datetime.now() - timedelta(hours=hours_ago),
    )


async def _seed_by_user(factory):
    """主种子: 5 个用户行组合 + 1 行 429 错误。

    期望:
    - alice: key11 ×2 (各 1000+200) + key12 ×1 (500+100) + legacy ×1 (100+50)
             + 429 ×1 (tokens None) → 5 req, 3150 tok
    - bob:   key13 ×1 (2000+400) → 1 req, 2400 tok
    - NULL caller: ×1 (50+25) → 1 req, 75 tok
    """
    await _reset_db(factory)
    await _seed_users_and_keys(factory)
    from db.models import UsageLog

    async with factory() as s:
        s.add_all([
            _log(1, 11, 1000, 200),
            _log(1, 11, 1000, 200),
            _log(1, 12, 500, 100),
            _log(1, None, 100, 50),          # legacy/env key 行
            _log(2, 13, 2000, 400, hours_ago=48),
            _log(None, None, 50, 25),         # 未鉴权/legacy caller
            # 429 错误行: by-user 无状态过滤会计入 (tokens None → 0), perf 会过滤
            _log(1, 11, None, None, status=429, lat=99999),
        ])
        await s.commit()


async def test_by_user_agg():
    """/api/usage/by-user: 聚合 + totals + key 下钻 (legacy + label 消歧)"""
    from db.engine import async_session_factory

    await _seed_by_user(async_session_factory)
    async with _get_client() as client:
        resp = await client.get("/dashboard/api/usage/by-user?days=7")
        assert resp.status_code == 200, resp.text
        data = resp.json()

    assert data["days"] == 7
    users = data["users"]
    assert len(users) == 3

    # 按 total_tokens desc: alice 3150 > bob 2400 > (未鉴权) 75
    alice, bob, legacy_user = users
    assert alice["username"] == "alice" and alice["display_name"] == "Alice"
    assert alice["request_count"] == 5, "429 行无状态过滤也计入"
    assert alice["total_tokens"] == 3150
    assert bob["username"] == "bob"
    assert bob["total_tokens"] == 2400
    assert legacy_user["username"] == "(未鉴权/legacy)"
    assert legacy_user["total_tokens"] == 75

    # alice 的 keys: 按 total_tokens desc: dsh(alice) 2400 > zcode 600 > legacy 150
    keys = alice["keys"]
    assert len(keys) == 3
    k0, k1, k2 = keys
    assert k0["label"] == "dsh (alice)", "同名 label 跨用户须追加 username 消歧"
    assert k0["key_id"] == 11 and k0["request_count"] == 3
    assert k1["label"] == "zcode" and k1["total_tokens"] == 600
    assert k2["key_id"] is None and k2["label"] == "(legacy/env)"
    assert k2["total_tokens"] == 150

    # bob 的 key13: 同名消歧 → "dsh (bob)"
    assert bob["keys"][0]["label"] == "dsh (bob)"

    # totals
    t = data["totals"]
    assert t["request_count"] == 7
    assert t["total_tokens"] == 3150 + 2400 + 75
    assert t["input_tokens"] == 1000 + 1000 + 500 + 100 + 2000 + 50
    assert t["output_tokens"] == 200 + 200 + 100 + 50 + 400 + 25


async def test_by_user_empty():
    """空 DB: users 空 + totals 全零"""
    from db.engine import async_session_factory

    await _reset_db(async_session_factory)
    async with _get_client() as client:
        resp = await client.get("/dashboard/api/usage/by-user?days=7")
        assert resp.status_code == 200
    data = resp.json()
    assert data["users"] == []
    assert data["totals"]["total_tokens"] == 0
    assert data["totals"]["request_count"] == 0


async def test_by_user_days_window():
    """days=1 窗口外的 bob 行不计入"""
    from db.engine import async_session_factory

    await _seed_by_user(async_session_factory)
    async with _get_client() as client:
        resp = await client.get("/dashboard/api/usage/by-user?days=1")
        data = resp.json()
    names = [u["username"] for u in data["users"]]
    assert "bob" not in names, "bob 唯一一行在 48h 前, days=1 应排除"
    assert "alice" in names


async def test_perf_dim_user():
    """perf dim=user: display_name 优先 + 429 过滤 + NULL caller 兜底"""
    from db.engine import async_session_factory

    await _seed_by_user(async_session_factory)
    async with _get_client() as client:
        resp = await client.get("/dashboard/api/usage/perf?days=7&dim=user")
        assert resp.status_code == 200
        data = resp.json()
    assert data["dim"] == "user"
    items = {it["key"]: it for it in data["items"]}

    assert "Alice" in items, "display_name 优先于 username"
    assert items["Alice"]["n"] == 4, "429 行被过滤 (5-1)"
    assert items["Bob"]["n"] == 1
    assert items["(未鉴权/legacy)"]["n"] == 1


async def test_perf_dim_key():
    """perf dim=key: label 消歧 + legacy 兜底 + 429 过滤"""
    from db.engine import async_session_factory

    await _seed_by_user(async_session_factory)
    async with _get_client() as client:
        resp = await client.get("/dashboard/api/usage/perf?days=7&dim=key")
        assert resp.status_code == 200
        data = resp.json()
    assert data["dim"] == "key"
    items = {it["key"]: it for it in data["items"]}

    assert items["dsh (alice)"]["n"] == 2, "key11 两行成功 (429 被过滤)"
    assert items["zcode"]["n"] == 1
    assert items["dsh (bob)"]["n"] == 1
    # alice legacy 行 + NULL caller legacy 行共用 key_id NULL 桶
    assert items["(legacy/env)"]["n"] == 2, "key_id NULL 两行归同一 legacy 桶"


async def test_user_trend_daily_buckets():
    """user-trend dim=user days=7: 按日分桶 + 补零 + series 降序"""
    from db.engine import async_session_factory

    await _seed_by_user(async_session_factory)
    async with _get_client() as client:
        resp = await client.get("/dashboard/api/usage/user-trend?days=7&dim=user")
        assert resp.status_code == 200
        data = resp.json()

    assert data["dim"] == "user"
    buckets = data["buckets"]
    # 滚动窗口含两端截段桶: cutoff.date()..today → days=7 实际 8 个日桶 (同 usage/summary 口径)
    assert len(buckets) == 8
    assert buckets[-1] == datetime.now().date().isoformat(), "末桶=今天"

    series = {s["key"]: s for s in data["series"]}
    assert set(series.keys()) == {"Alice", "Bob", "(未鉴权/legacy)"}
    assert data["series"][0]["key"] == "Alice", "series 按总 token 降序"

    # alice 全部在今天桶 (now-1h), bob 在 48h 前的桶 → 各自另一桶为 0 (补零)
    alice = series["Alice"]
    assert sum(alice["tokens"]) == 3150
    assert sum(alice["requests"]) == 5
    assert alice["tokens"][-1] == 3150, "alice 全部行在 1h 内 → 末桶"
    assert alice["tokens"][-3] == 0, "48h 前的桶 alice 补零"
    bob = series["Bob"]
    assert bob["tokens"][-1] == 0 and bob["tokens"][-3] == 2400


async def test_user_trend_hourly():
    """user-trend days=1: 小时桶轴 + 末桶对齐"""
    from db.engine import async_session_factory

    await _seed_by_user(async_session_factory)
    async with _get_client() as client:
        resp = await client.get("/dashboard/api/usage/user-trend?days=1&dim=user")
        assert resp.status_code == 200
        data = resp.json()

    buckets = data["buckets"]
    assert all(len(b) == 16 and b.endswith(":00") for b in buckets), "小时桶格式 YYYY-MM-DD HH:00"
    assert len(buckets) in (24, 25), "24h 窗口 24-25 个小时桶 (两端截段)"
    # bob 48h 前的行不在窗口 → series 无 Bob
    keys = {s["key"] for s in data["series"]}
    assert "Bob" not in keys
    assert "Alice" in keys


async def test_user_trend_key_dim_and_cap():
    """user-trend dim=key: key 维 + >8 系列时 top-8 + 其他 归并"""
    from db.engine import async_session_factory

    await _reset_db(async_session_factory)
    from db.models import User, ApiKey, UsageLog

    async with async_session_factory() as s:
        s.add(User(id=1, username="kuser", display_name="K User", role="user", status="active"))
        # 10 个 key, token 量 100, 200, ..., 1000 → top8 = 300..1000, 其他 = 100+200
        for i in range(1, 11):
            s.add(ApiKey(id=i, user_id=1, key_hash=f"h{i}", key_prefix=f"aimc-{i}",
                         is_admin=0, status="active", label=f"key-{i:02d}"))
            s.add(UsageLog(
                caller_id=1, key_id=i, model="m",
                input_tokens=i * 100, output_tokens=0,
                latency_ms=50, status_code=200,
                created_at=datetime.now() - timedelta(hours=1),
            ))
        # 2 个 legacy 行: 150 tok
        s.add(UsageLog(caller_id=1, key_id=None, model="m", input_tokens=150, output_tokens=0,
                       latency_ms=50, status_code=200, created_at=datetime.now() - timedelta(hours=1)))
        await s.commit()

    async with _get_client() as client:
        resp = await client.get("/dashboard/api/usage/user-trend?days=7&dim=key")
        assert resp.status_code == 200
        data = resp.json()

    series = data["series"]
    # 10 keys + 1 legacy = 11 个维度 > 8 → top-8 + 其他 = 9 个 series
    assert len(series) == 9, f"11 维度应截为 8+1=9, got {len(series)}"
    keys = [s["key"] for s in series]
    assert "其他" in keys and keys[-1] == "其他", "其他 桶排末位"
    # top8 = key-03..key-10 (300..1000); 其他 = key-01(100) + key-02(200) + legacy(150)
    assert "(legacy/env)" not in keys and "key-01" not in keys and "key-02" not in keys
    other = next(s for s in series if s["key"] == "其他")
    assert sum(other["tokens"]) == 450
    assert sum(other["requests"]) == 3
    # top1 = key-10 (1000)
    assert series[0]["key"] == "key-10" and sum(series[0]["tokens"]) == 1000


async def test_requests_caller_filter():
    """/api/requests caller_id 筛选 + 复合 status"""
    from db.engine import async_session_factory

    await _seed_by_user(async_session_factory)
    async with _get_client() as client:
        # alice (含 429)
        resp = await client.get("/dashboard/api/requests?days=7&caller_id=1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 5

        # alice + error → 只有 429 行
        resp = await client.get("/dashboard/api/requests?days=7&caller_id=1&status=error")
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["status_code"] == 429

        # 无 caller_id → 全部 7 行
        resp = await client.get("/dashboard/api/requests?days=7")
        data = resp.json()
        assert data["total"] == 7
