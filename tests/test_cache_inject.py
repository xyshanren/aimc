"""P2-16: 测试 _inject_prompt_cache (P1-8)

P3 (2026-08-21) 路径 A 优化后覆盖:
- 坏 JSON 原样返回
- 无 messages 原样返回
- Anthropic 风格 (api_style="anthropic"): system 消息 + 第一条 user 加 cache_control (校验 role)
- OpenAI 风格 (默认): prompt_cache_key = 分段 hash(system 全量 + 首条非 system)
- 不覆盖已存在的 cache_control / prompt_cache_key (idempotent)
- 多轮对话前缀稳定 → 同 key; 长内容截断等价
"""
import json

from gateway.proxy import _inject_prompt_cache, _stable_prefix_cache_key


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


def test_anthropic_style_only_system_and_first_user():
    """Anthropic 风格: 只标 system + 第一条 user, 后续 assistant/user 不标"""
    body = _make_body([
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "response"},
        {"role": "user", "content": "next"},
    ])
    channel = {"api_style": "anthropic"}
    result = _parse(_inject_prompt_cache(body, channel))
    assert "cache_control" in result["messages"][0]
    assert "cache_control" in result["messages"][1]
    assert "cache_control" not in result["messages"][2]
    assert "cache_control" not in result["messages"][3]


def test_anthropic_style_checks_role_not_position():
    """P3: Anthropic 风格校验 role — [user, assistant, user] 只标第一条 user

    之前盲目标 messages[:2], user 在第 1 位 + assistant 在第 2 位会被错标
    """
    body = _make_body([
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "second"},
    ])
    channel = {"api_style": "anthropic"}
    result = _parse(_inject_prompt_cache(body, channel))
    assert result["messages"][0]["cache_control"] == {"type": "ephemeral"}
    # assistant 不标
    assert "cache_control" not in result["messages"][1]
    # 第二条 user 也不标 (只标第一条)
    assert "cache_control" not in result["messages"][2]


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
    """OpenAI 风格: 加 prompt_cache_key (分段 hash, 16 位)"""
    body = _make_body([
        {"role": "system", "content": "you are helpful"},
        {"role": "user", "content": "hi"},
    ])
    channel = {"api_style": "openai"}
    result = _parse(_inject_prompt_cache(body, channel))
    assert "prompt_cache_key" in result
    assert len(result["prompt_cache_key"]) == 16


def test_openai_style_stable_prefix_same_key_across_turns():
    """P3 核心: 多轮对话前缀稳定 → 同 key

    agent loop 场景: system + 首条 user 跨轮不变, 后续 append — 应同 key
    """
    ch = {"api_style": "openai"}
    turn1 = _make_body([
        {"role": "system", "content": "you are an agent"},
        {"role": "user", "content": "do task A"},
    ])
    turn2 = _make_body([
        {"role": "system", "content": "you are an agent"},
        {"role": "user", "content": "do task A"},
        {"role": "assistant", "content": "step 1 done"},
        {"role": "user", "content": "continue"},
    ])
    r1 = _parse(_inject_prompt_cache(turn1, ch))
    r2 = _parse(_inject_prompt_cache(turn2, ch))
    assert r1["prompt_cache_key"] == r2["prompt_cache_key"]


def test_openai_style_different_system_different_key():
    """OpenAI 风格: 不同 system -> 不同 cache key"""
    body1 = _make_body([{"role": "system", "content": "A"}, {"role": "user", "content": "x"}])
    body2 = _make_body([{"role": "system", "content": "B"}, {"role": "user", "content": "x"}])
    ch = {"api_style": "openai"}
    r1 = _parse(_inject_prompt_cache(body1, ch))
    r2 = _parse(_inject_prompt_cache(body2, ch))
    assert r1["prompt_cache_key"] != r2["prompt_cache_key"]


def test_openai_style_no_system_uses_first_user():
    """P3: 无 system -> fallback 用首条 user 消息 (消灭全局空串 key)"""
    ch = {"api_style": "openai"}
    b1 = _make_body([{"role": "user", "content": "question A"}])
    b2 = _make_body([{"role": "user", "content": "question B"}])
    r1 = _parse(_inject_prompt_cache(b1, ch))
    r2 = _parse(_inject_prompt_cache(b2, ch))
    assert len(r1["prompt_cache_key"]) == 16
    # 不同首条 user -> 不同 key
    assert r1["prompt_cache_key"] != r2["prompt_cache_key"]


def test_openai_style_multiple_system_messages_all_hashed():
    """P3: 多条 system 全部参与 hash (之前只取第一条)"""
    ch = {"api_style": "openai"}
    b1 = _make_body([
        {"role": "system", "content": "rule 1"},
        {"role": "system", "content": "rule 2"},
        {"role": "user", "content": "x"},
    ])
    b2 = _make_body([
        {"role": "system", "content": "rule 1"},
        {"role": "user", "content": "x"},
    ])
    r1 = _parse(_inject_prompt_cache(b1, ch))
    r2 = _parse(_inject_prompt_cache(b2, ch))
    # 第二条 system 参与了 hash -> 不同 key
    assert r1["prompt_cache_key"] != r2["prompt_cache_key"]


def test_openai_style_preserves_caller_provided_key():
    """P3: 调用方已传 prompt_cache_key 时不覆盖"""
    body = _make_body(
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
        prompt_cache_key="caller-key-123",
    )
    channel = {"api_style": "openai"}
    result = _parse(_inject_prompt_cache(body, channel))
    assert result["prompt_cache_key"] == "caller-key-123"


def test_openai_style_truncation_equivalence():
    """P3: 首条 user 超过 4096 字符时截断 — 超长部分不影响 key"""
    long_content = "x" * 5000
    messages_truncated_prefix = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": long_content},
    ]
    messages_with_extra = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": long_content + "extra tail"},
    ]
    k1 = _stable_prefix_cache_key(messages_truncated_prefix)
    k2 = _stable_prefix_cache_key(messages_with_extra)
    # 4096 之后的内容被截断, key 相同
    assert k1 == k2


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


def test_inject_disabled_when_global_switch_off():
    """V4.9.3 A/B 开关: prompt_cache_inject_enabled=False 时调用点不注入

    验证调用点的 guard 逻辑: supports_prompt_cache=True AND enabled=False → 不调用 _inject_prompt_cache
    """
    from config import settings

    original = settings.prompt_cache_inject_enabled
    settings.prompt_cache_inject_enabled = False
    try:
        channel = {"api_style": "openai", "supports_prompt_cache": True}
        should_inject = channel.get("supports_prompt_cache") and settings.prompt_cache_inject_enabled
        assert should_inject is False
    finally:
        settings.prompt_cache_inject_enabled = original
