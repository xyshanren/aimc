"""2026-11-04 (P3) — 价行建议队列的 dashboard API 契约

覆盖:
1. GET /api/price-suggestions 返回 pending 建议 + 渠道名
2. POST approve → 落 price_snapshots + 刷该渠道路由缓存 (route_engine 打桩)
3. POST reject → 不动价行
4. 重复 approve 幂等 (status 已 approved 不再写库)

跑法: python -m pytest tests/test_intel_price_suggestion_api.py -q
"""

import asyncio
import sys
import types
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _install_mock_env():
    class _MockSettings:
        openai_api_key = "sk-test"
        openai_base_url = "https://mock.local/v1"
        parse_model = "gpt-4o-mini"
        parse_confidence_threshold = 0.6
        crawl_enabled = True
        aimc_master_key = "test-key"
        route_refresh_interval = 600
        db_url = "sqlite+aiosqlite:///:memory:"
        db_path = "./data/aimc.db"

    sys.modules.setdefault("config", types.ModuleType("config"))
    sys.modules["config"].settings = _MockSettings()


_install_mock_env()


@pytest.fixture()
async def db_session():
    """每用例独立 in-memory 库"""
    import warnings

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool

    from db.models import Base

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        await engine.dispose()


async def _seed(db_session):
    from db import crud, price_suggestions as ps

    ch = await crud.create_channel(
        db_session, name="test-deepseek", provider="deepseek",
        base_url="https://mock.local/v1", status="active",
    )
    await db_session.flush()
    await crud.create_price_snapshot(
        db_session, channel_id=ch.id, model="deepseek-flash",
        price_type="normal", input_price=1.0, output_price=4.0,
    )
    await db_session.flush()
    sug = await ps.suggest_price(
        db_session, ch.id, "deepseek-flash", 2.0, 8.0, source="crawler",
    )
    return ch, sug


def test_list_price_suggestions_endpoint(db_session):
    from dashboard.routes import get_price_suggestions

    async def _run():
        await _seed(db_session)
        return await get_price_suggestions(status="pending", limit=100, session=db_session)

    data = asyncio.run(_run())
    assert data["count"] == 1
    item = data["items"][0]
    assert item["channel_name"] == "test-deepseek"
    assert item["model"] == "deepseek-flash"
    assert (item["old_input_price"], item["old_output_price"]) == (1.0, 4.0)
    assert (item["input_price"], item["output_price"]) == (2.0, 8.0)


def test_approve_endpoint_writes_price_and_refreshes_cache(db_session, monkeypatch):
    import gateway.router as router_mod
    from dashboard.routes import approve_price_suggestion

    calls = []

    class _FakeEngine:
        async def invalidate_price(self, channel_id):
            calls.append(channel_id)

    monkeypatch.setattr(router_mod, "route_engine", _FakeEngine())

    async def _run():
        ch, sug = await _seed(db_session)
        res = await approve_price_suggestion(suggestion_id=sug.id, session=db_session)
        return ch, res

    ch, res = asyncio.run(_run())
    assert res["status"] == "approved"
    assert res["cache_refreshed"] is True
    assert calls == [ch.id]

    from db import price_suggestions as ps
    pending = asyncio.run(ps.list_suggestions(db_session, status="pending"))
    assert pending == [], "批准后不应再有 pending"


def test_reject_endpoint_keeps_price(db_session):
    from dashboard.routes import reject_price_suggestion
    from sqlalchemy import select
    from db.models import PriceSnapshot

    async def _run():
        ch, sug = await _seed(db_session)
        res = await reject_price_suggestion(suggestion_id=sug.id, session=db_session)
        rows = (await db_session.execute(
            select(PriceSnapshot).where(PriceSnapshot.channel_id == ch.id)
        )).scalars().all()
        return res, rows

    res, rows = asyncio.run(_run())
    assert res["status"] == "rejected"
    assert len(rows) == 1
    assert (rows[0].input_price, rows[0].output_price) == (1.0, 4.0)


def test_double_approve_is_idempotent(db_session, monkeypatch):
    import gateway.router as router_mod
    from dashboard.routes import approve_price_suggestion

    class _FakeEngine:
        async def invalidate_price(self, channel_id):
            return None

    monkeypatch.setattr(router_mod, "route_engine", _FakeEngine())

    async def _run():
        _ch, sug = await _seed(db_session)
        first = await approve_price_suggestion(suggestion_id=sug.id, session=db_session)
        second = await approve_price_suggestion(suggestion_id=sug.id, session=db_session)
        return first, second

    first, second = asyncio.run(_run())
    assert first["status"] == "approved"
    assert second["status"] == "approved"
    assert second["cache_refreshed"] is False, "重复批准不应再次写库/刷缓存"
