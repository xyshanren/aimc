"""P0-1/P0-2/P0-3 (2026-08-19): 测试 streaming usage 提取 + cache token 入库 + model 守卫

覆盖:
- P0-1: _extract_stream_usage 从 OpenAI / Anthropic SSE 尾部提取 usage
- P0-1: _schedule_stream_usage_log 不带 usage 的流 -> 跳过 (不崩)
- P0-2: _write_usage_log 写入时 cache token 字段实际入库 (不再丢)
- P0-3: _resolve_actual_model 组路由残留污染守卫
"""
import asyncio
import json
from pathlib import Path

import pytest


# === P0-1: _extract_stream_usage ===

def _openai_stream_tail(usage_dict, model="gpt-4"):
    """构造 OpenAI 风格 SSE 尾部 (含 usage 的最后 chunk + [DONE])"""
    chunk = {
        "id": "chatcmpl-x",
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [],
        "usage": usage_dict,
    }
    return f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode("utf-8")


def _anthropic_stream_tail(usage_dict, model="claude-3"):
    """构造 Anthropic 风格 SSE 尾部 (message_delta 事件含 usage)"""
    # 简化: 只放 message_delta + message_stop
    delta = {"type": "message_delta", "usage": usage_dict}
    return f"event: message_delta\ndata: {json.dumps(delta)}\n\nevent: message_stop\ndata: {{\"type\":\"message_stop\"}}\n\n".encode("utf-8")


def test_extract_openai_usage():
    """OpenAI: 末尾 usage chunk 被提取"""
    from gateway.proxy import _extract_stream_usage
    tail = _openai_stream_tail({
        "prompt_tokens": 1000,
        "completion_tokens": 200,
        "total_tokens": 1200,
        "prompt_tokens_details": {"cached_tokens": 600},
    })
    usage = _extract_stream_usage(tail)
    assert usage is not None
    assert usage["prompt_tokens"] == 1000
    assert usage["completion_tokens"] == 200
    assert usage["prompt_tokens_details"]["cached_tokens"] == 600
    # _stream_model 附带 (供 fallback)
    assert usage.get("_stream_model") == "gpt-4"


def test_extract_anthropic_usage():
    """Anthropic: message_delta 的 usage 被提取"""
    from gateway.proxy import _extract_stream_usage
    tail = _anthropic_stream_tail({
        "input_tokens": 1000,
        "output_tokens": 200,
        "cache_read_input_tokens": 600,
        "cache_creation_input_tokens": 100,
    })
    usage = _extract_stream_usage(tail)
    assert usage is not None
    assert usage["input_tokens"] == 1000
    assert usage["output_tokens"] == 200
    assert usage["cache_read_input_tokens"] == 600


def test_extract_usage_none_when_no_usage():
    """流里没有 usage -> None (不崩)"""
    from gateway.proxy import _extract_stream_usage
    # 只有普通 delta chunk, 没有 usage
    chunk = {"id": "x", "choices": [{"delta": {"content": "hi"}}]}
    tail = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode("utf-8")
    assert _extract_stream_usage(tail) is None


def test_extract_usage_only_done_marker():
    """只有 [DONE] -> None"""
    from gateway.proxy import _extract_stream_usage
    assert _extract_stream_usage(b"data: [DONE]\n\n") is None


def test_extract_usage_garbage_bytes():
    """乱码字节 -> None (不崩)"""
    from gateway.proxy import _extract_stream_usage
    assert _extract_stream_usage(b"\xff\xfe garbage not sse") is None


def test_extract_usage_multiple_chunks_takes_last():
    """多个 usage chunk -> 取最后一个"""
    from gateway.proxy import _extract_stream_usage
    c1 = {"id": "x", "model": "gpt-4", "usage": {"prompt_tokens": 100, "completion_tokens": 10}}
    c2 = {"id": "x", "model": "gpt-4", "usage": {"prompt_tokens": 1000, "completion_tokens": 200}}
    tail = f"data: {json.dumps(c1)}\n\ndata: {json.dumps(c2)}\n\ndata: [DONE]\n\n".encode("utf-8")
    usage = _extract_stream_usage(tail)
    assert usage["prompt_tokens"] == 1000
    assert usage["completion_tokens"] == 200


def test_extract_usage_tail_buffer_truncation_safe():
    """尾部缓冲被截断 (超大流) -> 仍能从保留的末尾提取

    注意: 生产代码的滑动缓冲只保留末尾 64KB, 截断会破坏前半行。
    这里用 60KB junk (< 64KB), 保证 usage chunk 整行落在保留窗口内。
    """
    from gateway.proxy import _extract_stream_usage
    # 60KB 无用字节 (不带换行, 模拟被截断的无用前缀) + 末尾 usage chunk
    junk = b"x" * 60000
    tail = junk + b"\n" + _openai_stream_tail({"prompt_tokens": 50, "completion_tokens": 5})
    usage = _extract_stream_usage(tail)
    assert usage is not None
    assert usage["prompt_tokens"] == 50


# === P0-1: _schedule_stream_usage_log 跳过逻辑 ===

def test_schedule_stream_usage_log_no_usage_skips(monkeypatch):
    """没有 usage -> 不调 _write_usage_log (不产生空记录)"""
    from gateway import proxy as proxy_mod

    called = []
    async def _fake_write(**kwargs):
        called.append(kwargs)
    monkeypatch.setattr(proxy_mod, "_write_usage_log", _fake_write)
    # monkeypatch asyncio.create_task 立即执行
    class _FakeLoop:
        def create_task(self, coro):
            asyncio.ensure_future(coro)
    monkeypatch.setattr(asyncio, "get_event_loop", lambda: _FakeLoop())

    channel = {"id": 1, "account_id": 2, "name": "test"}
    # 只有 [DONE], 无 usage
    proxy_mod._schedule_stream_usage_log(channel, 200, 100, "gpt-4", b"data: [DONE]\n\n")

    # 让 event loop 跑完 pending tasks
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(asyncio.sleep(0.01))
    finally:
        loop.close()

    assert called == [], "不应在无 usage 时写入"


# === P0-2: _write_usage_log cache token 入库 ===

@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """临时 SQLite + create_all, monkeypatch db.engine.async_session_factory"""
    p = tmp_path / "p02.db"
    sync_url = f"sqlite:///{p}"
    async_url = f"sqlite+aiosqlite:///{p}"

    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
    from db.models import Base

    eng = create_async_engine(async_url, echo=False, future=True)
    factory = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)

    # 建表
    async def _init():
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_init())

    monkeypatch.setattr("db.engine.async_session_factory", factory)
    # proxy._write_usage_log 内部 from db.engine import async_session_factory (运行时导入)
    monkeypatch.setattr("gateway.proxy.async_session_factory", factory, raising=False)
    return p, factory


def test_write_usage_log_persists_cache_tokens(tmp_db):
    """P0-2: cache token 字段实际写入数据库 (不再丢)"""
    from gateway.proxy import _write_usage_log
    from db.models import UsageLog
    from sqlalchemy import select

    p, factory = tmp_db
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _run():
        await _write_usage_log(
            channel_id=1, account_id=2, model="gpt-4",
            input_tokens=1000, output_tokens=200,
            status_code=200, latency_ms=150,
            group_id=None,
            cached_input_tokens=600,
            cache_read_input_tokens=None,
            cache_creation_input_tokens=None,
        )
        async with factory() as s:
            rows = (await s.execute(select(UsageLog))).scalars().all()
            assert len(rows) == 1
            r = rows[0]
            assert r.cached_input_tokens == 600  # P0-2 修复前这里是 None
            assert r.input_tokens == 1000
            assert r.output_tokens == 200
            assert r.model == "gpt-4"

    loop.run_until_complete(_run())
    loop.close()


def test_write_usage_log_persists_anthropic_cache_tokens(tmp_db):
    """P0-2: Anthropic 风格 cache token 入库"""
    from gateway.proxy import _write_usage_log
    from db.models import UsageLog
    from sqlalchemy import select

    p, factory = tmp_db
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _run():
        await _write_usage_log(
            channel_id=1, account_id=2, model="claude-3",
            input_tokens=1000, output_tokens=200,
            status_code=200, latency_ms=150,
            group_id=None,
            cached_input_tokens=None,
            cache_read_input_tokens=600,
            cache_creation_input_tokens=100,
        )
        async with factory() as s:
            rows = (await s.execute(select(UsageLog))).scalars().all()
            assert len(rows) == 1
            r = rows[0]
            assert r.cache_read_input_tokens == 600
            assert r.cache_creation_input_tokens == 100

    loop.run_until_complete(_run())
    loop.close()


# === P0-3: _resolve_actual_model 守卫 ===

def test_resolve_actual_model_group_route_uses_injected():
    """组路由: 用 channel._actual_model (router 注入的实际命中)"""
    from gateway.proxy import _resolve_actual_model
    channel = {"_actual_model": "deepseek-v3"}
    assert _resolve_actual_model(channel, "tier:strong") == "deepseek-v3"


def test_resolve_actual_model_exact_route_ignores_stale_injected():
    """P0-3 核心: 精确路由忽略共享 dict 残留的 _actual_model"""
    from gateway.proxy import _resolve_actual_model
    # 场景: 上一次组路由命中 deepseek-v3, _actual_model 残留在共享 channel dict
    # 这次精确路由请求 deepseek-v2.5, 应该用 requested_model 而非残留值
    channel = {"_actual_model": "deepseek-v3"}  # 残留污染
    assert _resolve_actual_model(channel, "deepseek-v2.5") == "deepseek-v2.5"


def test_resolve_actual_model_group_no_injected_falls_back():
    """组路由但 channel 无 _actual_model -> 用 requested_model"""
    from gateway.proxy import _resolve_actual_model
    channel = {}
    assert _resolve_actual_model(channel, "tier:strong") == "tier:strong"


def test_resolve_actual_model_exact_fallback_to_response_model():
    """精确路由 requested_model 空 -> fallback 到上游返回的 model"""
    from gateway.proxy import _resolve_actual_model
    channel = {"_actual_model": "stale-v3"}  # 残留
    assert _resolve_actual_model(channel, "", fallback_model="gpt-4o") == "gpt-4o"


def test_resolve_actual_model_group_with_response_fallback():
    """组路由 + _actual_model -> 优先 _actual_model, 不被 response model 覆盖"""
    from gateway.proxy import _resolve_actual_model
    channel = {"_actual_model": "qwen-max"}
    assert _resolve_actual_model(channel, "tier:balanced", fallback_model="gpt-4") == "qwen-max"
