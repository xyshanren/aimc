# -*- coding: utf-8 -*-
"""V6.10 /api/usage/perf 性能统计端点测试

覆盖:
1. dim=model: p50/p95 latency (nearest-rank) / eff tok/s / 缓存命中率 / TTFT
2. dim=channel: 按渠道聚合
3. 状态码过滤: 仅 2xx/3xx 计入 (4xx/5xx 排除)
4. ttft 覆盖: first_token_ms 非空行才参与均值; NULL → ttft_n=0

跑法: python -m pytest tests/test_v610_perf_endpoint.py -v
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
    tmp_dir = tempfile.mkdtemp(prefix="aimc_v610_perf_")
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


async def _seed(factory):
    """种子数据: 两个渠道/账号, 各若干成功请求 + 1 行错误(应被过滤).

    临时库每次 drop_all + create_all 重建 (不用 delete, 保持种子确定性).
    """
    from sqlalchemy.ext.asyncio import create_async_engine
    from db.models import Base
    from db.models import Channel, Account, UsageLog

    # 与 async_session_factory 同文件 db 重建表 (mock settings.db_path)
    from config import settings
    db_path = getattr(settings, "db_path", None) or "./_v610_perf.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await eng.dispose()

    async with factory() as s:
        ch_a = Channel(id=101, name="ch-a", provider="x", base_url="https://ch-a.mock", status="active")
        ch_b = Channel(id=102, name="ch-b", provider="y", base_url="https://ch-b.mock", status="active")
        acc_1 = Account(id=201, channel_id=101, account_name="acc-1", api_key="sk-fake-1", status="active")
        acc_2 = Account(id=202, channel_id=102, account_name="acc-2", api_key="sk-fake-2", status="active")
        s.add_all([ch_a, ch_b, acc_1, acc_2])
        await s.commit()
        now = datetime.now()
        # ch-a / acc-1 / model-a: lat 100/200/300/400, in=1000 out=100 each,
        # cache_read=600 each, 1 行带 first_token_ms=50
        for i, lat in enumerate([100, 200, 300, 400]):
            s.add(UsageLog(
                channel_id=101, account_id=201, model="model-a",
                input_tokens=1000, output_tokens=100,
                cache_read_input_tokens=600,
                first_token_ms=50 if i == 0 else None,
                latency_ms=lat, status_code=200,
                created_at=now - timedelta(hours=1),
            ))
        # ch-b / acc-2 / model-b: 2 行小延迟
        for lat in [50, 150]:
            s.add(UsageLog(
                channel_id=102, account_id=202, model="model-b",
                input_tokens=500, output_tokens=80,
                latency_ms=lat, status_code=200,
                created_at=now - timedelta(hours=1),
            ))
        # 错误请求: 应被 2xx/3xx 过滤
        s.add(UsageLog(
            channel_id=101, account_id=201, model="model-a",
            input_tokens=1000, output_tokens=0,
            latency_ms=99999, status_code=429,
            error_type="quota_exhausted",
            created_at=now - timedelta(hours=1),
        ))
        await s.commit()


async def test_perf_by_model():
    """dim=model: p50/p95/eff_tok_per_s/cache_pct/ttft"""
    from db.engine import async_session_factory

    await _seed(async_session_factory)
    async with _get_client() as client:
        resp = await client.get("/dashboard/api/usage/perf?days=7&dim=model")
        assert resp.status_code == 200
        data = resp.json()
        assert data["dim"] == "model"
        items = {it["key"]: it for it in data["items"]}

    a = items.get("model-a")
    assert a is not None
    assert a["n"] == 4, "429 行应被过滤, 只留 4 个成功"
    # nearest-rank: 0.50*4=2 → idx2=300; 0.95*4=3 → idx3=400
    assert a["p50_latency_ms"] == 300
    assert a["p95_latency_ms"] == 400
    assert a["avg_latency_ms"] == 250.0
    # eff tok/s = Σout 400 / Σlat 1.0s = 400
    assert a["eff_tok_per_s"] == 400.0
    # cache: 600*4 / 1000*4 = 60%
    assert a["cache_pct"] == 60.0
    # ttft: 仅 1 行非空 → 50ms, 覆盖 1/4
    assert a["ttft_ms"] == 50.0
    assert a["ttft_n"] == 1


async def test_perf_by_channel_and_status_filter():
    """dim=channel: 按渠道聚合; 状态码过滤正确"""
    from db.engine import async_session_factory

    await _seed(async_session_factory)
    async with _get_client() as client:
        resp = await client.get("/dashboard/api/usage/perf?days=7&dim=channel")
        items = {it["key"]: it for it in resp.json()["items"]}

    assert items["ch-a"]["n"] == 4
    assert items["ch-b"]["n"] == 2
    # ch-b: Σout 160 / Σlat 0.2s = 800
    assert items["ch-b"]["eff_tok_per_s"] == 800.0
    assert items["ch-b"]["ttft_n"] == 0
    assert items["ch-b"]["ttft_ms"] is None