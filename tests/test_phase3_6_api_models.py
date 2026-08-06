"""Phase 3.6 (2026-08-06) — API 结构化抽取单元测试

覆盖 (api_models.py):
1. 标准 OpenAI 格式: {"data": [{"id": "..."}]} → list[str]
2. 兼容 GLM/DeepSeek 变体: {"model": [{"id": "..."}]}
3. 401 不重试, 直接 raise (含 status_code)
4. 500 重试 3 次后 raise
5. 200 但 data 空 → raise
6. 带 api_key → Bearer header
7. URL 末尾 / 自动去掉
8. _resolve_api_key happy path (mock channels + decrypt)
9. _resolve_api_key 无 channel → None

跑法: python tests/test_phase3_6_api_models.py
"""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ============================================================
# Mock config
# ============================================================
def _install_mock_config():
    import types

    class _MockSettings:
        openai_api_key = "sk-test"
        openai_base_url = "https://mock.local/v1"
        parse_model = "gpt-4o-mini"
        parse_confidence_threshold = 0.6
        crawl_enabled = True
        aimc_master_key = "0" * 64  # 64 字符 hex (mock crypto 用)
        route_refresh_interval = 600
        db_url = "sqlite+aiosqlite:///:memory:"
        db_path = "./data/aimc.db"

    sys.modules.setdefault("config", types.ModuleType("config"))
    sys.modules["config"].settings = _MockSettings()


_install_mock_config()


# ============================================================
# Test 1: 标准 OpenAI 格式
# ============================================================
def test_fetch_api_openai_standard():
    print("\n=== Test 1: 标准 OpenAI 格式 ===")
    from intelligence.sources.api_models import fetch_models_via_api
    import httpx

    sample = {
        "object": "list",
        "data": [
            {"id": "Qwen/Qwen2.5-7B-Instruct", "object": "model"},
            {"id": "deepseek-ai/DeepSeek-V2.5", "object": "model"},
        ],
    }

    def _handler(request: httpx.Request) -> httpx.Response:
        # 确认 URL 末尾 /models 被自动补
        assert str(request.url) == "https://api.siliconflow.cn/v1/models", \
            f"URL 应为 /v1/models, 实际 {request.url}"
        # 确认 UA
        assert "aimc" in request.headers["user-agent"]
        # 确认无 Authorization (没传 key)
        assert "authorization" not in {k.lower() for k in request.headers.keys()}
        return httpx.Response(200, json=sample)

    transport = httpx.MockTransport(_handler)

    async def _run():
        # 用 transport 替换默认
        orig_client = httpx.AsyncClient
        httpx.AsyncClient = lambda *a, **kw: orig_client(transport=transport, *a, **kw)
        try:
            return await fetch_models_via_api(
                base_url="https://api.siliconflow.cn/v1",
                api_key=None,
            )
        finally:
            httpx.AsyncClient = orig_client

    models = asyncio.run(_run())
    assert models == ["Qwen/Qwen2.5-7B-Instruct", "deepseek-ai/DeepSeek-V2.5"], (
        f"应原序返回, 实际 {models}"
    )
    print(f"  [OK] 标准 OpenAI 格式: {models}")


# ============================================================
# Test 2: GLM/DeepSeek 变体 (model 字段)
# ============================================================
def test_fetch_api_glm_variant():
    print("\n=== Test 2: GLM/DeepSeek model 字段变体 ===")
    from intelligence.sources.api_models import fetch_models_via_api
    import httpx

    sample = {
        "model": [
            {"id": "glm-4-plus"},
            {"id": "glm-4-flash"},
        ],
    }

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=sample)

    transport = httpx.MockTransport(_handler)

    async def _run():
        orig_client = httpx.AsyncClient
        httpx.AsyncClient = lambda *a, **kw: orig_client(transport=transport, *a, **kw)
        try:
            return await fetch_models_via_api(
                base_url="https://open.bigmodel.cn/api/paas/v4",
            )
        finally:
            httpx.AsyncClient = orig_client

    models = asyncio.run(_run())
    assert models == ["glm-4-plus", "glm-4-flash"]
    print(f"  [OK] GLM 变体 model 字段: {models}")


# ============================================================
# Test 3: 401 不重试, raise FetchApiError
# ============================================================
def test_fetch_api_401_no_retry():
    print("\n=== Test 3: 401 不重试 ===")
    from intelligence.sources.api_models import fetch_models_via_api, FetchApiError
    import httpx

    call_count = {"n": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(
            401,
            json={"error": {"message": "Invalid API key", "type": "auth_error"}},
        )

    transport = httpx.MockTransport(_handler)

    async def _run():
        orig_client = httpx.AsyncClient
        httpx.AsyncClient = lambda *a, **kw: orig_client(transport=transport, *a, **kw)
        try:
            try:
                await fetch_models_via_api(
                    base_url="https://api.deepseek.com/v1",
                    api_key="bad-key",
                )
                return None
            except FetchApiError as e:
                return e
        finally:
            httpx.AsyncClient = orig_client

    err = asyncio.run(_run())
    assert err is not None, "应 raise FetchApiError"
    assert err.status_code == 401
    assert call_count["n"] == 1, f"401 不应重试, 实际调用 {call_count['n']} 次"
    print(f"  [OK] 401 只调用 1 次, status_code={err.status_code}")


# ============================================================
# Test 4: 500 重试 3 次后 raise
# ============================================================
def test_fetch_api_500_retries_3_times():
    print("\n=== Test 4: 500 重试 3 次后 raise ===")
    from intelligence.sources.api_models import fetch_models_via_api, FetchApiError
    import httpx

    call_count = {"n": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(500, text="Internal Server Error")

    transport = httpx.MockTransport(_handler)

    async def _run():
        orig_client = httpx.AsyncClient
        httpx.AsyncClient = lambda *a, **kw: orig_client(
            transport=transport,
            timeout=kw.get("timeout", 30),
            **{k: v for k, v in kw.items() if k != "timeout"},
        )
        try:
            try:
                # 缩短退避时间, 测试更快
                await fetch_models_via_api(
                    base_url="https://api.example.com/v1",
                    base_delay=0.01,
                    backoff=2.0,
                )
                return None
            except FetchApiError as e:
                return e
        finally:
            httpx.AsyncClient = orig_client

    err = asyncio.run(_run())
    assert err is not None
    assert err.status_code == 500
    assert call_count["n"] == 3, f"应重试 3 次, 实际 {call_count['n']} 次"
    print(f"  [OK] 500 重试 3 次后 raise, 调用 {call_count['n']} 次")


# ============================================================
# Test 5: 200 但 data 空 → raise
# ============================================================
def test_fetch_api_200_empty_data():
    print("\n=== Test 5: 200 但 data 空 ===")
    from intelligence.sources.api_models import fetch_models_via_api, FetchApiError
    import httpx

    def _handler(request: httpx.Request) -> httpx.Response:
        # data 字段是空列表
        return httpx.Response(200, json={"object": "list", "data": []})

    transport = httpx.MockTransport(_handler)

    async def _run():
        orig_client = httpx.AsyncClient
        httpx.AsyncClient = lambda *a, **kw: orig_client(transport=transport, *a, **kw)
        try:
            try:
                await fetch_models_via_api(base_url="https://api.test.com/v1")
                return None
            except FetchApiError as e:
                return e
        finally:
            httpx.AsyncClient = orig_client

    err = asyncio.run(_run())
    assert err is not None
    assert err.status_code == 200
    print(f"  [OK] 空 data 也 raise, status_code={err.status_code}")


# ============================================================
# Test 6: 带 api_key → Bearer header
# ============================================================
def test_fetch_api_with_bearer_key():
    print("\n=== Test 6: 带 api_key 鉴权头 ===")
    from intelligence.sources.api_models import fetch_models_via_api
    import httpx

    captured_headers = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        # 抓 header (httpx Request.headers 大小写不敏感)
        for k, v in request.headers.items():
            captured_headers[k.lower()] = v
        return httpx.Response(200, json={"data": [{"id": "test-model"}]})

    transport = httpx.MockTransport(_handler)

    async def _run():
        orig_client = httpx.AsyncClient
        httpx.AsyncClient = lambda *a, **kw: orig_client(transport=transport, *a, **kw)
        try:
            return await fetch_models_via_api(
                base_url="https://api.qwen.com/v1",
                api_key="sk-abc123",
            )
        finally:
            httpx.AsyncClient = orig_client

    models = asyncio.run(_run())
    assert models == ["test-model"]
    assert captured_headers.get("authorization") == "Bearer sk-abc123", (
        f"Bearer 鉴权头错, 实际 {captured_headers}"
    )
    print(f"  [OK] Bearer 鉴权头: {captured_headers.get('authorization')}")


# ============================================================
# Test 7: URL 末尾 / 自动去掉
# ============================================================
def test_fetch_api_url_trailing_slash():
    print("\n=== Test 7: URL 末尾 / 自动化处理 ===")
    from intelligence.sources.api_models import fetch_models_via_api
    import httpx

    captured_url = {"url": ""}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured_url["url"] = str(request.url)
        return httpx.Response(200, json={"data": [{"id": "m1"}]})

    transport = httpx.MockTransport(_handler)

    async def _run():
        orig_client = httpx.AsyncClient
        httpx.AsyncClient = lambda *a, **kw: orig_client(transport=transport, *a, **kw)
        try:
            # 末尾多个 /
            await fetch_models_via_api(base_url="https://api.test.com/v1///")
        finally:
            httpx.AsyncClient = orig_client

    asyncio.run(_run())
    # 不应有双斜杠
    assert captured_url["url"] == "https://api.test.com/v1/models", (
        f"URL 应规整, 实际 {captured_url['url']}"
    )
    print(f"  [OK] 末尾 /// → /v1/models: {captured_url['url']}")


# ============================================================
# Test 8: _resolve_api_key happy path
# ============================================================
def test_resolve_api_key_happy():
    print("\n=== Test 8: _resolve_api_key happy path ===")
    from intelligence.scheduler import _resolve_api_key
    from security.crypto import encrypt

    # mock channel
    fake_channel = MagicMock()
    fake_channel.api_key_ciphertext = encrypt("sk-test-key-12345", "0" * 64)

    fake_session = MagicMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)

    async def _fake_list_active(session, provider):
        return [fake_channel]

    fake_source = MagicMock()
    fake_source.api_key_from = "siliconflow"

    with patch("db.engine.async_session_factory", return_value=fake_session), \
         patch("db.crud.list_active_channels_by_provider", _fake_list_active):
        result = asyncio.run(_resolve_api_key(fake_source))

    assert result == "sk-test-key-12345", f"应返回明文 key, 实际 {result}"
    print(f"  [OK] 解密 key 成功: {result[:8]}...")


# ============================================================
# Test 9: _resolve_api_key 无 channel → None
# ============================================================
def test_resolve_api_key_no_channel():
    print("\n=== Test 9: _resolve_api_key 无 channel ===")
    from intelligence.scheduler import _resolve_api_key

    fake_session = MagicMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)

    async def _fake_empty_list(session, provider):
        return []

    fake_source = MagicMock()
    fake_source.api_key_from = "unknown_provider"

    with patch("db.engine.async_session_factory", return_value=fake_session), \
         patch("db.crud.list_active_channels_by_provider", _fake_empty_list):
        result = asyncio.run(_resolve_api_key(fake_source))

    assert result is None, f"无 channel 应返 None, 实际 {result}"
    print(f"  [OK] 无 channel 返 None (走公开 endpoint)")


# ============================================================
# Test 10: _resolve_api_key api_key_from=None → None (短路)
# ============================================================
def test_resolve_api_key_no_provider():
    print("\n=== Test 10: _resolve_api_key 无 api_key_from ===")
    from intelligence.scheduler import _resolve_api_key

    fake_source = MagicMock()
    fake_source.api_key_from = None

    result = asyncio.run(_resolve_api_key(fake_source))
    assert result is None
    print("  [OK] api_key_from=None 短路返 None")


# ============================================================
# Test 11: IntelSourceBase 新字段生效 (不破坏现有 dataclass)
# ============================================================
def test_intel_source_base_new_fields():
    print("\n=== Test 11: IntelSourceBase api_endpoint / api_key_from ===")
    from intelligence.sources.base import IntelSourceBase
    from intelligence.sources.model_lists import (
        SiliconFlowModelListSource,
        VolcengineArkModelListSource,
    )

    sf = SiliconFlowModelListSource()
    assert sf.api_endpoint == "https://api.siliconflow.cn/v1"
    assert sf.api_key_from == "siliconflow"

    ark = VolcengineArkModelListSource()
    assert ark.api_endpoint == "https://ark.cn-beijing.volces.com/api/v3"
    assert ark.api_key_from == "volcengine"

    # 老字段 (向后兼容)
    assert sf.url == "https://docs.siliconflow.cn/cn-userguide/quickstart"
    assert sf.css_selector == "article, main, body"

    # 默认值 (无 api_endpoint 的 source)
    from intelligence.sources.deepseek import DeepSeekSource
    ds = DeepSeekSource()
    assert ds.api_endpoint is None
    assert ds.api_key_from is None
    print("  [OK] 新字段生效 + 老字段兼容 + 默认值正确")


# ============================================================
# Main
# ============================================================
def main():
    print("=" * 60)
    print("Phase 3.6 — API 结构化抽取 测试")
    print("=" * 60)

    test_fetch_api_openai_standard()
    test_fetch_api_glm_variant()
    test_fetch_api_401_no_retry()
    test_fetch_api_500_retries_3_times()
    test_fetch_api_200_empty_data()
    test_fetch_api_with_bearer_key()
    test_fetch_api_url_trailing_slash()
    test_resolve_api_key_happy()
    test_resolve_api_key_no_channel()
    test_resolve_api_key_no_provider()
    test_intel_source_base_new_fields()

    print("\n" + "=" * 60)
    print("ALL 11 TESTS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    main()