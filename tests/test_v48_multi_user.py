"""V4.8 (2026-08-19): 多用户鉴权 + caller_id 链路测试

测试范围:
1. middleware 多 key 鉴权 (api_keys 表命中 / legacy 回退 / 无效 key 拒绝)
2. caller_id 链路: middleware -> request.state -> routes -> proxy -> usage_logs
3. _write_usage_log 写入 caller_id 到数据库
4. 鉴权缓存 (30s TTL, 负缓存)
"""

import asyncio
import hashlib
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# 项目根目录
ROOT = Path(__file__).parent.parent.resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ============================================================
# Helper: 创建临时 DB + 初始化 users/api_keys 表
# ============================================================

@pytest.fixture
def v48_db(tmp_path, monkeypatch):
    """临时 SQLite, 含 users + api_keys + usage_logs 表"""
    db_file = tmp_path / "v48_test.db"
    async_url = f"sqlite+aiosqlite:///{db_file}"

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
    return db_file, factory, eng


def _seed_user_and_key(factory, username="alice", is_admin=False, key_plain="test-key-alice-12345"):
    """在 DB 里建一个 user + api_key, 返回 (user_id, key_id, key_plain)"""
    async def _seed():
        from db.models import User, ApiKey
        async with factory() as s:
            u = User(username=username, display_name=username.title(), role="admin" if is_admin else "user", status="active")
            s.add(u)
            await s.flush()
            key_hash = hashlib.sha256(key_plain.encode()).hexdigest()
            k = ApiKey(
                user_id=u.id, key_hash=key_hash, key_prefix=key_plain[:8],
                is_admin=1 if is_admin else 0, status="active", label="test",
            )
            s.add(k)
            await s.flush()
            await s.commit()
            return u.id, k.id
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    result = loop.run_until_complete(_seed())
    loop.close()
    return result + (key_plain,)


# ============================================================
# Test 1: middleware 多 key 鉴权 — api_keys 表命中
# ============================================================

def test_middleware_multikey_hit(v48_db):
    """V4.8: api_keys 表有匹配 key -> 鉴权通过, 设 caller_id"""
    db_file, factory, eng = v48_db
    user_id, key_id, key_plain = _seed_user_and_key(factory)

    # 清缓存 (跨 test 隔离) + patch middleware 运行时导入的 session factory
    import gateway.middleware as mw
    import db.engine as dbeng
    mw._apikey_cache.clear()
    orig_factory = dbeng.async_session_factory
    dbeng.async_session_factory = factory

    async def _run():
        result = await mw._lookup_api_key(key_plain)
        assert result is not None
        assert result.user_id == user_id
        assert result.username == "alice"
        assert result.is_admin == False
        assert result.key_id == key_id

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_run())
    loop.close()
    dbeng.async_session_factory = orig_factory
# ============================================================

def test_middleware_multikey_miss(v48_db):
    """V4.8: api_keys 表无匹配 key -> 返回 None"""
    db_file, factory, eng = v48_db
    _seed_user_and_key(factory)

    import gateway.middleware as mw
    import db.engine as dbeng
    mw._apikey_cache.clear()
    orig_factory = dbeng.async_session_factory
    dbeng.async_session_factory = factory

    async def _run():
        result = await mw._lookup_api_key("nonexistent-key-xyz")
        assert result is None

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_run())
    loop.close()
    dbeng.async_session_factory = orig_factory


# ============================================================
# Test 3: middleware 多 key 鉴权 — revoked key 被拒
# ============================================================

def test_middleware_multikey_revoked(v48_db):
    """V4.8: status=revoked 的 key -> 返回 None"""
    db_file, factory, eng = v48_db
    user_id, key_id, key_plain = _seed_user_and_key(factory)

    # 把 key 改成 revoked
    async def _revoke():
        from db.models import ApiKey
        from sqlalchemy import update
        async with factory() as s:
            await s.execute(update(ApiKey).where(ApiKey.id == key_id).values(status="revoked"))
            await s.commit()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_revoke())
    loop.close()

    import gateway.middleware as mw
    import db.engine as dbeng
    mw._apikey_cache.clear()
    orig_factory = dbeng.async_session_factory
    dbeng.async_session_factory = factory

    async def _run():
        result = await mw._lookup_api_key(key_plain)
        assert result is None  # revoked key 不应命中

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_run())
    loop.close()
    dbeng.async_session_factory = orig_factory


# ============================================================
# Test 4: middleware 多 key 鉴权 — disabled user 的 key 被拒
# ============================================================

def test_middleware_multikey_disabled_user(v48_db):
    """V4.8: user.status=disabled -> key 失效"""
    db_file, factory, eng = v48_db
    user_id, key_id, key_plain = _seed_user_and_key(factory)

    async def _disable():
        from db.models import User
        from sqlalchemy import update
        async with factory() as s:
            await s.execute(update(User).where(User.id == user_id).values(status="disabled"))
            await s.commit()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_disable())
    loop.close()

    import gateway.middleware as mw
    import db.engine as dbeng
    mw._apikey_cache.clear()
    orig_factory = dbeng.async_session_factory
    dbeng.async_session_factory = factory

    async def _run():
        result = await mw._lookup_api_key(key_plain)
        assert result is None

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_run())
    loop.close()
    dbeng.async_session_factory = orig_factory


# ============================================================
# Test 5: 鉴权缓存 — 第二次查走缓存
# ============================================================

def test_middleware_cache_hit(v48_db):
    """V4.8: 同一个 key 第二次查 -> 走缓存 (不查 DB)"""
    db_file, factory, eng = v48_db
    user_id, key_id, key_plain = _seed_user_and_key(factory)

    import gateway.middleware as mw
    import db.engine as dbeng
    mw._apikey_cache.clear()
    orig_factory = dbeng.async_session_factory
    dbeng.async_session_factory = factory

    async def _run():
        # 第一次查 (走 DB)
        result1 = await mw._lookup_api_key(key_plain)
        assert result1 is not None

        # mock _query_api_key 验证第二次不调 DB
        original = mw._query_api_key
        call_count = [0]
        async def _counted(key_hash):
            call_count[0] += 1
            return await original(key_hash)
        mw._query_api_key = _counted

        # 第二次查 (应走缓存)
        result2 = await mw._lookup_api_key(key_plain)
        assert result2 is not None
        assert result2.user_id == user_id
        assert call_count[0] == 0  # 缓存命中, 不查 DB

        mw._query_api_key = original

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_run())
    loop.close()
    dbeng.async_session_factory = orig_factory


# ============================================================
# Test 6: 鉴权缓存 — 负缓存 (无效 key 不反复查 DB)
# ============================================================

def test_middleware_negative_cache(v48_db):
    """V4.8: 无效 key 也缓存 (负缓存), 不反复查 DB"""
    db_file, factory, eng = v48_db
    _seed_user_and_key(factory)

    import gateway.middleware as mw
    import db.engine as dbeng
    mw._apikey_cache.clear()
    orig_factory = dbeng.async_session_factory
    dbeng.async_session_factory = factory

    async def _run():
        # 第一次查 (走 DB, 返回 None)
        result1 = await mw._lookup_api_key("invalid-key")
        assert result1 is None

        # mock 验证第二次不调 DB
        original = mw._query_api_key
        call_count = [0]
        async def _counted(key_hash):
            call_count[0] += 1
            return await original(key_hash)
        mw._query_api_key = _counted

        # 第二次查 (应走负缓存)
        result2 = await mw._lookup_api_key("invalid-key")
        assert result2 is None
        assert call_count[0] == 0

        mw._query_api_key = original

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_run())
    loop.close()
    dbeng.async_session_factory = orig_factory


# ============================================================
# Test 7: _write_usage_log 写入 caller_id
# ============================================================

def test_write_usage_log_with_caller_id(v48_db):
    """V4.8: _write_usage_log 写入 caller_id 到 usage_logs"""
    db_file, factory, eng = v48_db
    user_id, key_id, key_plain = _seed_user_and_key(factory)

    async def _run():
        from gateway.proxy import _write_usage_log
        from db.models import UsageLog
        from sqlalchemy import select

        await _write_usage_log(
            channel_id=1, account_id=2, model="gpt-4",
            input_tokens=100, output_tokens=50,
            status_code=200, latency_ms=100,
            group_id=None,
            caller_id=user_id,
        )
        async with factory() as s:
            rows = (await s.execute(select(UsageLog))).scalars().all()
            assert len(rows) == 1
            r = rows[0]
            assert r.caller_id == user_id
            assert r.model == "gpt-4"
            assert r.input_tokens == 100

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_run())
    loop.close()


# ============================================================
# Test 8: _write_usage_log caller_id 默认 None (legacy)
# ============================================================

def test_write_usage_log_caller_id_default_none(v48_db):
    """V4.8: 不传 caller_id -> 默认 None (legacy 数据)"""
    db_file, factory, eng = v48_db

    async def _run():
        from gateway.proxy import _write_usage_log
        from db.models import UsageLog
        from sqlalchemy import select

        await _write_usage_log(
            channel_id=1, account_id=2, model="gpt-4",
            input_tokens=100, output_tokens=50,
            status_code=200, latency_ms=100,
        )
        async with factory() as s:
            rows = (await s.execute(select(UsageLog))).scalars().all()
            assert len(rows) == 1
            assert rows[0].caller_id is None

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_run())
    loop.close()


# ============================================================
# Test 9: admin key — is_admin=True
# ============================================================

def test_middleware_admin_key(v48_db):
    """V4.8: admin key -> is_admin=True"""
    db_file, factory, eng = v48_db
    user_id, key_id, key_plain = _seed_user_and_key(
        factory, username="boss", is_admin=True, key_plain="admin-key-boss-99999"
    )

    import gateway.middleware as mw
    import db.engine as dbeng
    mw._apikey_cache.clear()
    orig_factory = dbeng.async_session_factory
    dbeng.async_session_factory = factory

    async def _run():
        result = await mw._lookup_api_key(key_plain)
        assert result is not None
        assert result.is_admin == True
        assert result.username == "boss"

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_run())
    loop.close()
    dbeng.async_session_factory = orig_factory
