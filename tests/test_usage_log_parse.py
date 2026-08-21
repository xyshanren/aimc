"""P2-16: 测试 cached_tokens 解析逻辑 (P0-3 follow-up)

覆盖:
- OpenAI 风格: usage.prompt_tokens_details.cached_tokens
- Anthropic 风格: usage.cache_read_input_tokens + cache_creation_input_tokens
- 异常 / 缺失字段 -> None (不崩)
- type 检查: 非 int -> 忽略

设计: 直接复用 _schedule_usage_log 内部的解析逻辑, 构造 mock response
"""
import asyncio
import json

import httpx

from gateway.proxy import _write_usage_log


def _make_mock_response(usage_dict, status_code=200, model="gpt-4"):
    """构造 httpx.Response mock"""
    body = json.dumps({
        "id": "test-123",
        "object": "chat.completion",
        "model": model,
        "choices": [],
        "usage": usage_dict,
    }).encode("utf-8")
    return httpx.Response(
        status_code=status_code,
        content=body,
        headers={"content-type": "application/json"},
    )


def _extract_cached_tokens(response):
    """从 response 提取 cached tokens (直接调生产 _parse_cache_tokens, 防逻辑漂移)

    P3 fix (2026-08-21): 之前这里复制了一份解析逻辑, 生产代码改动时测试不会跟着变
    """
    from gateway.proxy import _parse_cache_tokens
    usage = response.json().get("usage", {})
    return _parse_cache_tokens(usage)


# === OpenAI 风格 ===
def test_openai_cache_hit_tokens():
    """OpenAI: prompt_tokens_details.cached_tokens 被解析"""
    usage = {
        "prompt_tokens": 1000,
        "completion_tokens": 200,
        "total_tokens": 1200,
        "prompt_tokens_details": {"cached_tokens": 600},
    }
    resp = _make_mock_response(usage)
    cached, read, created = _extract_cached_tokens(resp)
    assert cached == 600
    assert read is None
    assert created is None


def test_openai_cache_hit_zero():
    """OpenAI: cached_tokens=0 也应解析 (不视作 None)"""
    usage = {
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "prompt_tokens_details": {"cached_tokens": 0},
    }
    resp = _make_mock_response(usage)
    cached, _, _ = _extract_cached_tokens(resp)
    assert cached == 0


def test_openai_cache_non_int_ignored():
    """OpenAI: cached_tokens 不是 int 忽略 (不崩)"""
    usage = {
        "prompt_tokens_details": {"cached_tokens": "600"},  # string 而非 int
    }
    resp = _make_mock_response(usage)
    cached, _, _ = _extract_cached_tokens(resp)
    assert cached is None


def test_openai_no_prompt_details():
    """OpenAI: 没 prompt_tokens_details -> None"""
    usage = {"prompt_tokens": 100, "completion_tokens": 50}
    resp = _make_mock_response(usage)
    cached, _, _ = _extract_cached_tokens(resp)
    assert cached is None


def test_openai_prompt_details_not_dict():
    """OpenAI: prompt_tokens_details 不是 dict (异常) -> None"""
    usage = {"prompt_tokens_details": "invalid"}
    resp = _make_mock_response(usage)
    cached, _, _ = _extract_cached_tokens(resp)
    assert cached is None


# === Anthropic 风格 ===
def test_anthropic_cache_read_and_creation():
    """Anthropic: cache_read_input_tokens + cache_creation_input_tokens"""
    usage = {
        "input_tokens": 1000,
        "output_tokens": 200,
        "cache_read_input_tokens": 600,
        "cache_creation_input_tokens": 100,
    }
    resp = _make_mock_response(usage)
    cached, read, created = _extract_cached_tokens(resp)
    # Anthropic 没有 prompt_tokens_details -> cached_input_tokens=None
    assert cached is None
    assert read == 600
    assert created == 100


def test_anthropic_cache_read_zero():
    """Anthropic: cache_read_input_tokens=0 仍解析"""
    usage = {
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    resp = _make_mock_response(usage)
    cached, read, created = _extract_cached_tokens(resp)
    assert read == 0
    assert created == 0


def test_anthropic_no_cache_fields():
    """Anthropic: 没 cache_read/creation -> None, None"""
    usage = {"input_tokens": 100, "output_tokens": 50}
    resp = _make_mock_response(usage)
    cached, read, created = _extract_cached_tokens(resp)
    assert cached is None
    assert read is None
    assert created is None


def test_anthropic_non_int_ignored():
    """Anthropic: cache_read 不是 int -> None"""
    usage = {"cache_read_input_tokens": "abc"}
    resp = _make_mock_response(usage)
    _, read, _ = _extract_cached_tokens(resp)
    assert read is None


# === 通用 ===
def test_no_usage_field():
    """response.json() 里没 usage 字段 -> 全 None (不崩)"""
    body = json.dumps({"id": "x", "model": "gpt-4"}).encode("utf-8")
    resp = httpx.Response(200, content=body, headers={"content-type": "application/json"})
    cached, read, created = _extract_cached_tokens(resp)
    assert cached is None
    assert read is None
    assert created is None


def test_empty_usage():
    """usage = {} -> 全 None"""
    resp = _make_mock_response({})
    cached, read, created = _extract_cached_tokens(resp)
    assert cached is None
    assert read is None
    assert created is None


def test_extract_group_id_prefix():
    """_extract_group_id: tier:/scene: 前缀 -> 原样返回"""
    from gateway.proxy import _extract_group_id
    assert _extract_group_id("tier:flagship") == "tier:flagship"
    assert _extract_group_id("scene:code") == "scene:code"


def test_extract_group_id_strips_model_suffix():
    """P3 fix: tier:xxx/model 后缀剥离 (与 router.py select_channel 对齐)

    之前 tier:vision/glm-4.5v 原样入库, dashboard 显示两行
    """
    from gateway.proxy import _extract_group_id
    assert _extract_group_id("tier:vision/glm-4.5v") == "tier:vision"
    assert _extract_group_id("tier:balanced/some-model") == "tier:balanced"


def test_extract_group_id_exact_model():
    """_extract_group_id: 精确模型名 -> None"""
    from gateway.proxy import _extract_group_id
    assert _extract_group_id("gpt-4") is None
    assert _extract_group_id("deepseek-ai/DeepSeek-V3") is None
    assert _extract_group_id("") is None
