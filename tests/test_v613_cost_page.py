# -*- coding: utf-8 -*-
"""V6.13 成本页整体设计 — 端点测试

覆盖:
1. /api/usage/cost-by-channel: 渠道维成本 + NULL 渠道兜底 + totals
2. /api/usage/cost-by-model-channels: 模型→渠道交叉 (下钻), 模型级 = 各渠道之和
3. /api/usage/cost-compare: 当前窗口 vs 等长上一窗口 (环比)
4. /api/usage/cost-by-user: reasoning_tokens 字段 (修复"思考"死列)
5. /api/cache/stats 精确化: 计费一致公式 + 免费渠道不计收益 (回归守卫) + 覆盖率

跑法: python -m pytest tests/test_v613_cost_page.py -v
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
    tmp_dir = tempfile.mkdtemp(prefix="aimc_v613_cost_")
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


async def _reset_db():
    from sqlalchemy.ext.asyncio import create_async_engine
    from db.models import Base
    from config import settings

    eng = create_async_engine(f"sqlite+aiosqlite:///{settings.db_path}")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await eng.dispose()


async def _seed_cost(factory):
    """成本维种子: ch1/ch2 各承载 model-a, 另有 ch1 上的 model-b + 一行 legacy(NULL channel).

    标价 (list_cost_estimate) 只给部分行, 用于覆盖率断言。
    """
    from db.models import Channel, UsageLog

    async with factory() as s:
        s.add_all([
            Channel(id=1, name="ch-paid", provider="x", base_url="https://a.mock", status="active"),
            Channel(id=2, name="ch-free", provider="y", base_url="https://b.mock", status="active"),
        ])
        await s.commit()
        now = datetime.now()
        rows = [
            # model-a 在 ch1: 10 请求, 实付 1.0, 标价 10.0
            dict(channel_id=1, model="model-a", request=10, cost=1.0, list_cost=10.0),
            # model-a 在 ch2: 5 请求, 实付 0.0 (免费), 标价 5.0
            dict(channel_id=2, model="model-a", request=5, cost=0.0, list_cost=5.0),
            # model-b 在 ch1: 3 请求, 实付 0.3, 标价未知 (NULL)
            dict(channel_id=1, model="model-b", request=3, cost=0.3, list_cost=None),
            # legacy: NULL channel (1 请求, 实付 0.05)
            dict(channel_id=None, model="model-a", request=1, cost=0.05, list_cost=None),
        ]
        for r in rows:
            for _ in range(r["request"]):
                s.add(UsageLog(
                    channel_id=r["channel_id"], model=r["model"],
                    input_tokens=100, output_tokens=20,
                    reasoning_tokens=7 if r["model"] == "model-a" else None,
                    cost_estimate=r["cost"] / r["request"],
                    list_cost_estimate=(r["list_cost"] / r["request"]) if r["list_cost"] is not None else None,
                    latency_ms=100, status_code=200,
                    created_at=now - timedelta(hours=1),
                ))
        await s.commit()


async def test_cost_by_channel():
    """渠道维: 两活跃渠道 + NULL 渠道兜底 + totals 与全窗口一致"""
    from db.engine import async_session_factory

    await _reset_db()
    await _seed_cost(async_session_factory)
    async with _get_client() as client:
        resp = await client.get("/dashboard/api/usage/cost-by-channel?days=7")
        assert resp.status_code == 200, resp.text
        data = resp.json()

    items = {it["channel_name"]: it for it in data["items"]}
    assert set(items.keys()) == {"ch-paid", "ch-free", "(未知渠道)"}
    # ch-paid: model-a(10) + model-b(3) = 13 请求, 实付 1.0+0.3
    assert items["ch-paid"]["request_count"] == 13
    assert abs(items["ch-paid"]["cost_yuan"] - 1.3) < 1e-6
    assert items["ch-free"]["request_count"] == 5
    assert items["ch-free"]["cost_yuan"] == 0.0
    assert items["(未知渠道)"]["request_count"] == 1
    # 排序: 标价降序 → ch-paid(15.0) > ch-free(5.0) > 未知(0)
    assert data["items"][0]["channel_name"] == "ch-paid"
    # totals = 全窗口
    t = data["totals"]
    assert t["request_count"] == 19
    assert abs(t["cost_yuan"] - 1.35) < 1e-6
    assert abs(t["list_cost_yuan"] - 15.0) < 1e-6
    assert t["list_known_requests"] == 15
    assert t["list_coverage_pct"] == round(15 / 19 * 100, 1)


async def test_cost_by_model_channels_drilldown():
    """模型→渠道下钻: 嵌套 channels 存在, 模型级 = 各渠道之和 (与 cost-by-model 同值)"""
    from db.engine import async_session_factory

    await _reset_db()
    await _seed_cost(async_session_factory)
    async with _get_client() as client:
        drill = (await client.get("/dashboard/api/usage/cost-by-model-channels?days=7")).json()
        flat = (await client.get("/dashboard/api/usage/cost-by-model?days=7")).json()

    drill_items = {it["model"]: it for it in drill["items"]}
    flat_items = {it["model"]: it for it in flat["items"]}

    ma = drill_items["model-a"]
    # model-a 挂在 ch-paid/ch-free/legacy 三处 → 3 个渠道子行
    assert len(ma["channels"]) == 3
    names = [c["channel_name"] for c in ma["channels"]]
    assert set(names) == {"ch-paid", "ch-free", "(未知渠道)"}
    # 模型级 = 渠道求和
    assert ma["request_count"] == sum(c["request_count"] for c in ma["channels"]) == 16
    assert abs(ma["cost_yuan"] - sum(c["cost_yuan"] for c in ma["channels"])) < 1e-6
    # 与 cost-by-model 同值 (下钻不改变口径)
    assert ma["request_count"] == flat_items["model-a"]["request_count"]
    assert abs(ma["cost_yuan"] - flat_items["model-a"]["cost_yuan"]) < 1e-6
    assert abs(ma["saved_yuan"] - flat_items["model-a"]["saved_yuan"]) < 1e-6
    # model-b 只有 ch-paid 一个渠道
    assert len(drill_items["model-b"]["channels"]) == 1


async def test_cost_compare_windows():
    """环比: 当前窗口取本窗口行, previous 取上一等长窗口"""
    from db.engine import async_session_factory
    from db.models import UsageLog

    await _reset_db()
    now = datetime.now()
    async with async_session_factory() as s:
        # 当前窗口 (近 7 天): 实付 2.0, 标价 20.0
        s.add(UsageLog(channel_id=None, model="m", input_tokens=100, output_tokens=0,
                       cost_estimate=2.0, list_cost_estimate=20.0, status_code=200,
                       created_at=now - timedelta(days=1)))
        # 上一窗口 (7~14 天前): 实付 1.0, 标价 4.0
        s.add(UsageLog(channel_id=None, model="m", input_tokens=50, output_tokens=0,
                       cost_estimate=1.0, list_cost_estimate=4.0, status_code=200,
                       created_at=now - timedelta(days=9)))
        # 更早 (14 天前): 两个窗口都不该收
        s.add(UsageLog(channel_id=None, model="m", input_tokens=999, output_tokens=0,
                       cost_estimate=99.0, list_cost_estimate=99.0, status_code=200,
                       created_at=now - timedelta(days=20)))
        await s.commit()

    async with _get_client() as client:
        resp = await client.get("/dashboard/api/usage/cost-compare?days=7")
        assert resp.status_code == 200, resp.text
        data = resp.json()

    assert data["days"] == 7
    cur, prev = data["current"], data["previous"]
    assert cur["request_count"] == 1 and abs(cur["cost_yuan"] - 2.0) < 1e-6
    assert abs(cur["saved_yuan"] - 18.0) < 1e-6
    assert cur["input_tokens"] == 100
    assert prev["request_count"] == 1 and abs(prev["cost_yuan"] - 1.0) < 1e-6
    assert abs(prev["saved_yuan"] - 3.0) < 1e-6
    assert prev["input_tokens"] == 50
    assert cur["list_coverage_pct"] == 100.0


async def test_cost_by_user_has_reasoning_tokens():
    """回归: 成本页「按用户」tab 的思考列 → cost-by-user 必须返回 reasoning_tokens"""
    from db.engine import async_session_factory
    from db.models import User, UsageLog

    await _reset_db()
    async with async_session_factory() as s:
        s.add(User(id=1, username="u1", display_name="U One", role="user", status="active"))
        await s.commit()
        s.add(UsageLog(caller_id=1, key_id=None, model="m", input_tokens=100, output_tokens=10,
                       reasoning_tokens=42, cost_estimate=0.1, list_cost_estimate=1.0,
                       status_code=200, created_at=datetime.now() - timedelta(hours=1)))
        await s.commit()

    async with _get_client() as client:
        data = (await client.get("/dashboard/api/usage/cost-by-user?days=7")).json()

    assert "reasoning_tokens" in data["items"][0], "该字段此前缺失导致前端思考列恒为 —"
    assert data["items"][0]["reasoning_tokens"] == 42


async def test_cache_stats_precise_and_no_fabrication():
    """Cache 精确化: 有钱渠道按真实差额算; 免费/无缓存价渠道计 0 (不再跨渠道外推)"""
    from db.engine import async_session_factory
    from db.models import Channel, PriceSnapshot, UsageLog

    await _reset_db()
    async with async_session_factory() as s:
        s.add_all([
            Channel(id=1, name="ch-paid", provider="x", base_url="https://a.mock", status="active"),
            Channel(id=2, name="ch-free", provider="y", base_url="https://b.mock", status="active"),
        ])
        await s.commit()
        # ch-paid: 输入价 1.0, 缓存价 0.1 → 每 token 省 0.9/1e6
        # 同键两行 (旧价 + 新价; 唯一约束含 time_range 故用不同时段) → 应以 effective_from 最新者为准
        s.add_all([
            PriceSnapshot(channel_id=1, model="m1", price_type="offpeak", time_range="00:00-08:00",
                          input_price=2.0, output_price=4.0, cached_input_price=0.5,
                          effective_from=datetime.now() - timedelta(days=30)),
            PriceSnapshot(channel_id=1, model="m1", price_type="normal", time_range="全天",
                          input_price=1.0, output_price=4.0, cached_input_price=0.1,
                          effective_from=datetime.now() - timedelta(days=1)),
            # ch-free: 免费 (输入价 0), 且无缓存价列
            PriceSnapshot(channel_id=2, model="m1", price_type="normal", time_range="全天",
                          input_price=0.0, output_price=0.0, cached_input_price=None,
                          effective_from=datetime.now() - timedelta(days=1)),
        ])
        now = datetime.now() - timedelta(hours=1)
        # ch-paid: 输入 1M, 命中 500k
        s.add(UsageLog(channel_id=1, model="m1", input_tokens=1_000_000, cached_input_tokens=500_000,
                       output_tokens=0, status_code=200, created_at=now))
        # ch-free: 输入 2M, 命中 1M (免费渠道命中不产生缓存折扣)
        s.add(UsageLog(channel_id=2, model="m1", input_tokens=2_000_000, cached_input_tokens=1_000_000,
                       output_tokens=0, status_code=200, created_at=now))
        await s.commit()

    async with _get_client() as client:
        resp = await client.get("/dashboard/api/cache/stats?days=7")
        assert resp.status_code == 200, resp.text
        data = resp.json()

    by_ch = {c["channel_name"]: c for c in data["by_channel"]}
    # ch-paid: 500k × (1.0 − 0.1) / 1e6 = 0.45 (且用最新价, 而非旧价的 0.5 → 若取旧价会得 0.75)
    assert abs(by_ch["ch-paid"]["saved_cost_cny"] - 0.45) < 1e-6, by_ch["ch-paid"]
    assert by_ch["ch-paid"]["priced_pct"] == 100.0
    # ch-free: 无缓存价 → 0 (回归守卫: 旧算法会用别的渠道价把 1M 命中 token 折出真金白银)
    assert by_ch["ch-free"]["saved_cost_cny"] == 0.0, "免费/无缓存价渠道不得虚增收益"
    assert by_ch["ch-free"]["priced_pct"] == 0.0
    assert by_ch["ch-free"]["cached_tokens"] == 1_000_000

    # totals
    assert data["total_cached_tokens"] == 1_500_000
    assert data["hit_rate_pct"] == 50.0
    assert abs(data["saved_cost_cny"] - 0.45) < 1e-6
    assert data["priced_cached_tokens"] == 500_000
    assert data["priced_pct"] == round(500_000 / 1_500_000 * 100, 1)

    # by_model: 聚合两渠道, 收益只来自有钱渠道
    m1 = [m for m in data["by_model"] if m["model"] == "m1"][0]
    assert m1["cached_tokens"] == 1_500_000
    assert abs(m1["saved_cost_cny"] - 0.45) < 1e-6
    assert m1["priced_pct"] == round(500_000 / 1_500_000 * 100, 1)


async def test_cache_stats_channel_own_price_not_other_channel():
    """同模型在两渠道各有价: 各渠道用自己的缓存价 (不互相污染)"""
    from db.engine import async_session_factory
    from db.models import Channel, PriceSnapshot, UsageLog

    await _reset_db()
    async with async_session_factory() as s:
        s.add_all([
            Channel(id=1, name="chA", provider="x", base_url="https://a.mock", status="active"),
            Channel(id=2, name="chB", provider="y", base_url="https://b.mock", status="active"),
        ])
        await s.commit()
        s.add_all([
            # chA: 差 8.0/1e6 (10 − 2)
            PriceSnapshot(channel_id=1, model="m1", price_type="normal", time_range="全天",
                          input_price=10.0, output_price=0.0, cached_input_price=2.0,
                          effective_from=datetime.now() - timedelta(days=1)),
            # chB: 差 0.9/1e6 (1.0 − 0.1)
            PriceSnapshot(channel_id=2, model="m1", price_type="normal", time_range="全天",
                          input_price=1.0, output_price=0.0, cached_input_price=0.1,
                          effective_from=datetime.now() - timedelta(days=1)),
        ])
        now = datetime.now() - timedelta(hours=1)
        s.add(UsageLog(channel_id=1, model="m1", input_tokens=1_000_000, cached_input_tokens=1_000_000,
                       output_tokens=0, status_code=200, created_at=now))
        s.add(UsageLog(channel_id=2, model="m1", input_tokens=1_000_000, cached_input_tokens=1_000_000,
                       output_tokens=0, status_code=200, created_at=now))
        await s.commit()

    async with _get_client() as client:
        data = (await client.get("/dashboard/api/cache/stats?days=7")).json()

    by_ch = {c["channel_name"]: c for c in data["by_channel"]}
    assert abs(by_ch["chA"]["saved_cost_cny"] - 8.0) < 1e-6
    assert abs(by_ch["chB"]["saved_cost_cny"] - 0.9) < 1e-6
    assert abs(data["saved_cost_cny"] - 8.9) < 1e-6
