# -*- coding: utf-8 -*-
"""V6.10 first_token_ms (TTFT) 采集链路测试

覆盖:
1. _write_usage_log 接受 first_token_ms kwarg 并真实落库 (NULL 默认兼容旧调用)
2. _schedule_stream_usage_log 把 first_token_ms 透传给 _write_usage_log
3. 历史调用无 first_token_ms → 列保持 NULL (不破坏存量路径)

跑法: python -m pytest tests/test_v610_first_token_ms.py -v
"""

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent.resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """临时 SQLite + create_all, monkeypatch async_session_factory (同 test_p0_usage_fixes)"""
    p = tmp_path / "v610.db"
    async_url = f"sqlite+aiosqlite:///{p}"

    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
    from db.models import Base

    eng = create_async_engine(async_url, echo=False, future=True)
    factory = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)

    async def _init():
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_init())

    monkeypatch.setattr("db.engine.async_session_factory", factory)
    monkeypatch.setattr("gateway.proxy.async_session_factory", factory, raising=False)
    return p, factory


def test_write_usage_log_persists_first_token_ms(tmp_db):
    """_write_usage_log 新 kwarg 落库; 存量字段不受影响"""
    from gateway.proxy import _write_usage_log
    from db.models import UsageLog
    from sqlalchemy import select

    p, factory = tmp_db
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _run():
        await _write_usage_log(
            channel_id=8, account_id=12, model="glm-5.3-flash",
            input_tokens=1000, output_tokens=500,
            status_code=200, latency_ms=30000,
            first_token_ms=2500,
        )
        async with factory() as s:
            rows = (await s.scalars(select(UsageLog))).all()
            assert len(rows) == 1
            r = rows[0]
            assert r.first_token_ms == 2500
            assert r.latency_ms == 30000
            assert r.model == "glm-5.3-flash"

    loop.run_until_complete(_run())
    loop.close()


def test_write_usage_log_first_token_defaults_none(tmp_db):
    """旧调用 (不带 first_token_ms) → NULL, 不破坏存量写入路径"""
    from gateway.proxy import _write_usage_log
    from db.models import UsageLog
    from sqlalchemy import select

    p, factory = tmp_db
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _run():
        await _write_usage_log(
            channel_id=5, account_id=3, model="deepseek-v4-flash",
            input_tokens=100, output_tokens=50,
            status_code=200, latency_ms=800,
        )
        async with factory() as s:
            r = (await s.scalars(select(UsageLog))).first()
            assert r.first_token_ms is None

    loop.run_until_complete(_run())
    loop.close()


async def test_schedule_stream_usage_log_passes_first_token(monkeypatch):
    """_schedule_stream_usage_log 把 first_token_ms 原样透传给 _write_usage_log"""
    from gateway import proxy as proxy_mod

    captured = []
    async def _fake_write(**kwargs):
        captured.append(kwargs)
    monkeypatch.setattr(proxy_mod, "_write_usage_log", _fake_write)

    channel = {"id": 8, "account_id": 12, "name": "taotoken_coding"}
    tail = (
        b'data: {"id":"x","model":"glm-5.3-flash","usage":'
        b'{"prompt_tokens":100,"completion_tokens":10}}\n\ndata: [DONE]\n\n'
    )
    proxy_mod._schedule_stream_usage_log(
        channel, 200, 30000, "tier:strong", tail,
        first_token_ms=2500,
    )
    await asyncio.sleep(0.05)  # 让 _spawn 的 task 完成

    assert captured, "应写入 usage_logs"
    assert captured[0].get("first_token_ms") == 2500


async def test_schedule_stream_usage_log_error_path_first_token_none(monkeypatch):
    """错误流 (status>=400, 无 usage) → first_token_ms 保持 None"""
    from gateway import proxy as proxy_mod

    captured = []
    async def _fake_write(**kwargs):
        captured.append(kwargs)
    monkeypatch.setattr(proxy_mod, "_write_usage_log", _fake_write)

    channel = {"id": 8, "account_id": 12, "name": "taotoken_coding"}
    err_body = b'{"error":{"message":"deepseek_for_coding requires active Coding Plan quota"}}'
    proxy_mod._schedule_stream_usage_log(channel, 429, 900, "tier:strong", err_body)
    await asyncio.sleep(0.05)

    assert captured, "失败请求也应落 usage_logs"
    assert captured[0].get("first_token_ms") is None
    assert captured[0]["status_code"] == 429