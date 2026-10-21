"""V6.18 (2026-10-22): 失败链端到端落库测试 (JSON 列序列化)。

配套 tests/test_v618_400_failover_chain.py — 那里验证行为, 这里验证
failed_channels 经 ORM 真正写进 sqlite 并能读回 (JSON 列序列化正确)。
"""

import pytest


@pytest.mark.asyncio
async def test_failed_channels_roundtrip_to_db(tmp_path, monkeypatch):
    """失败链经 ORM 落库并能读回 (JSON 列序列化正确)。"""
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
    from db.models import Base, UsageLog

    db_file = tmp_path / "v618_chain.db"
    eng = create_async_engine("sqlite+aiosqlite:///" + str(db_file), echo=False, future=True)
    factory = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)

    async def _init():
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    await _init()

    monkeypatch.setattr("db.engine.async_session_factory", factory)
    monkeypatch.setattr("gateway.proxy.async_session_factory", factory, raising=False)

    from gateway import proxy

    chain = [
        {"channel": "sensenova_official", "account": "lixiaoy", "account_id": 7,
         "status": 400, "error_type": "client_error", "error_code": "BadRequest",
         "error_msg": "inference request is invalid", "needs_human": False},
        {"channel": "minimax_compat", "account": "minimax_01", "account_id": 8,
         "status": 500, "error_type": "upstream_5xx", "error_code": None,
         "error_msg": "Upstream error", "needs_human": False},
    ]

    await proxy._write_usage_log(
        channel_id=1, account_id=7, model="sensenova-6.8-flash-lite",
        input_tokens=0, output_tokens=0, status_code=400, latency_ms=686,
        error_msg="inference request is invalid", error_type="client_error",
        error_code="BadRequest", needs_human=False,
        failed_channels=chain,
    )

    async with factory() as s:
        row = await s.get(UsageLog, 1)
        assert row is not None, "应写入一行 usage_log"
        assert row.error_type == "client_error"
        assert row.error_code == "BadRequest"
        assert row.failed_channels is not None
        assert len(row.failed_channels) == 2
        assert row.failed_channels[0]["channel"] == "sensenova_official"
        assert row.failed_channels[0]["status"] == 400
        assert row.failed_channels[1]["error_type"] == "upstream_5xx"

    await eng.dispose()


@pytest.mark.asyncio
async def test_failed_channels_none_is_sql_null(tmp_path, monkeypatch):
    """无失败链的行落 SQL NULL, 不是 JSON 'null' 字符串。

    ORM 用 `Column(JSON(none_as_null=True))` — 否则 SQLAlchemy 默认把
    Python None 序列化成 JSON 字面量 'null' (typeof='text'), 将来
    "failed_channels IS NOT NULL" 的查询会把全部成功请求算成有失败链。
    """
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
    from db.models import Base, UsageLog

    # 断言 ORM 列配置 (纯 Python — ORM 读回时 JSON 'null' 与 SQL NULL
    # 都反序列化成 None, 只有存储层 typeof 才区分得开; 数据层校验交给
    # 迁移脚本 db/migrations/2026_10_22_v6x_failed_channels_null.py)
    col = UsageLog.__table__.c.failed_channels
    assert col.type.none_as_null is True, \
        "failed_channels 列必须用 JSON(none_as_null=True), 否则 None 落成字符串 'null'"

    db_file = tmp_path / "v618_null.db"
    eng = create_async_engine("sqlite+aiosqlite:///" + str(db_file), echo=False, future=True)
    factory = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)

    async def _init():
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    await _init()

    monkeypatch.setattr("db.engine.async_session_factory", factory)
    monkeypatch.setattr("gateway.proxy.async_session_factory", factory, raising=False)

    from gateway import proxy

    # 无失败链 (None)
    await proxy._write_usage_log(
        channel_id=1, account_id=2, model="m1",
        input_tokens=0, output_tokens=0, status_code=200, latency_ms=10,
    )

    async with factory() as s:
        row = await s.get(UsageLog, 1)
        assert row is not None, "应写入一行 usage_log"
        assert row.failed_channels is None, \
            f"无失败链时应为 None, 实际 {row.failed_channels!r}"

    await eng.dispose()
