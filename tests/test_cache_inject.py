"""P2-16: 测试 _inject_prompt_cache (P1-8)

覆盖:
- 坏 JSON 原样返回
- 无 messages 原样返回
- Anthropic 风格 (api_style="anthropic"): 加 cache_control 到 system + 第一个 user
- OpenAI 风格 (默认): 加 prompt_cache_key (system content hash[:16])
- 不覆盖已存在的 cache_control (idempotent)
- OpenAI 风格下, 提取 system content (string 或 list-of-parts)
"""
import json

from gateway.proxy import _inject_prompt_cache


def _make_body(messages, model="gpt-4", **extra):
    body = {"model": model, "messages": messages, **extra}
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def _parse(body):
    return json.loads(body.decode("utf-8"))


def test_invalid_json_returns_unchanged():
    """坏 JSON 原样返回 (不抛异常)"""
    body = b"not json"
    channel = {"api_style": "openai"}
    assert _inject_prompt_cache(body, channel) == body


def test_no_messages_returns_unchanged():
    body = _make_body([])  # empty
    channel = {"api_style": "openai"}
    assert _inject_prompt_cache(body, channel) == body


def test_anthropic_style_adds_cache_control_to_system():
    """Anthropic 风格: 给 system + 第一个 user 加 cache_control"""
    body = _make_body([
        {"role": "system", "content": "you are helpful"},
        {"role": "user", "content": "hi"},
    ])
    channel = {"api_style": "anthropic"}
    result = _parse(_inject_prompt_cache(body, channel))
    assert result["messages"][0]["cache_control"] == {"type": "ephemeral"}
    assert result["messages"][1]["cache_control"] == {"type": "ephemeral"}


def test_anthropic_style_skips_after_two_messages():
    """Anthropic 风格: 只前 2 个 msg 加 cache_control, 后续不加"""
    body = _make_body([
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "response"},
        {"role": "user", "content": "next"},
    ])
    channel = {"api_style": "anthropic"}
    result = _parse(_inject_prompt_cache(body, channel))
    # 前 2 个有 cache_control
    assert "cache_control" in result["messages"][0]
    assert "cache_control" in result["messages"][1]
    # 后 2 个没有
    assert "cache_control" not in result["messages"][2]
    assert "cache_control" not in result["messages"][3]


def test_anthropic_style_idempotent():
    """已有 cache_control 不覆盖"""
    body = _make_body([
        {"role": "system", "content": "sys",
         "cache_control": {"type": "existing"}},
    ])
    channel = {"api_style": "anthropic"}
    result = _parse(_inject_prompt_cache(body, channel))
    assert result["messages"][0]["cache_control"] == {"type": "existing"}


def test_openai_style_adds_prompt_cache_key():
    """OpenAI 风格: 加 prompt_cache_key (system content hash[:16])"""
    body = _make_body([
        {"role": "system", "content": "you are helpful"},
        {"role": "user", "content": "hi"},
    ])
    channel = {"api_style": "openai"}
    result = _parse(_inject_prompt_cache(body, channel))
    assert "prompt_cache_key" in result
    assert len(result["prompt_cache_key"]) == 16
    # 同样 system content 应得同样 key (deterministic)
    body2 = _make_body([
        {"role": "system", "content": "you are helpful"},
        {"role": "user", "content": "different"},  # user 变了
    ])
    result2 = _parse(_inject_prompt_cache(body2, channel))
    assert result["prompt_cache_key"] == result2["prompt_cache_key"]


def test_openai_style_different_system_different_key():
    """OpenAI 风格: 不同 system content -> 不同 cache key"""
    body1 = _make_body([{"role": "system", "content": "A"}, {"role": "user", "content": "x"}])
    body2 = _make_body([{"role": "system", "content": "B"}, {"role": "user", "content": "x"}])
    ch = {"api_style": "openai"}
    r1 = _parse(_inject_prompt_cache(body1, ch))
    r2 = _parse(_inject_prompt_cache(body2, ch))
    assert r1["prompt_cache_key"] != r2["prompt_cache_key"]


def test_openai_style_no_system_message_uses_empty_hash():
    """无 system message -> cache key 是空字符串 hash"""
    body = _make_body([{"role": "user", "content": "hi"}])
    channel = {"api_style": "openai"}
    result = _parse(_inject_prompt_cache(body, channel))
    # 空 system_content 的 hash (deterministic, 16 chars)
    assert len(result["prompt_cache_key"]) == 16


def test_openai_style_system_content_as_list():
    """OpenAI 风格: system content 是 list of parts (提取所有 text 拼接)"""
    body = _make_body([
        {"role": "system", "content": [
            {"type": "text", "text": "part 1"},
            {"type": "text", "text": "part 2"},
        ]},
        {"role": "user", "content": "hi"},
    ])
    channel = {"api_style": "openai"}
    result1 = _parse(_inject_prompt_cache(body, channel))
    # 等价于 system content = "part 1part 2"
    body2 = _make_body([
        {"role": "system", "content": "part 1part 2"},
        {"role": "user", "content": "hi"},
    ])
    result2 = _parse(_inject_prompt_cache(body2, channel))
    assert result1["prompt_cache_key"] == result2["prompt_cache_key"]


def test_default_api_style_is_openai():
    """未指定 api_style -> 默认 OpenAI 风格"""
    body = _make_body([
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ])
    channel = {}  # api_style 缺失
    result = _parse(_inject_prompt_cache(body, channel))
    assert "prompt_cache_key" in result
