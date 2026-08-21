"""P2-B (2026-08-20): drop_params (deny-list per channel) 测试

覆盖:
- _parse_drop_params: JSON list / 逗号分隔 / 空 / None
- _drop_params: 基本剥离 + ALWAYS_KEEP_PARAMS 保护 + channel 无配置早返回
- 非法 JSON body 安全跳过

跑法:
    pytest tests/test_drop_params.py -v
"""
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ============================================================
# _parse_drop_params
# ============================================================

def test_parse_json_list_string():
    """JSON list string → list[str]"""
    from gateway.proxy import _parse_drop_params
    assert _parse_drop_params('["reasoning_effort","max_tokens"]') == \
        ["reasoning_effort", "max_tokens"]


def test_parse_comma_separated():
    """逗号分隔 string → list[str] (dashboard 表单输入)"""
    from gateway.proxy import _parse_drop_params
    assert _parse_drop_params("reasoning_effort,max_tokens") == \
        ["reasoning_effort", "max_tokens"]


def test_parse_single_value():
    """单个值 (无逗号) → 单元素 list"""
    from gateway.proxy import _parse_drop_params
    assert _parse_drop_params("reasoning_effort") == ["reasoning_effort"]


def test_parse_strips_whitespace():
    """前后空格 + 元素间空格被 strip"""
    from gateway.proxy import _parse_drop_params
    assert _parse_drop_params("  reasoning_effort , max_tokens  ") == \
        ["reasoning_effort", "max_tokens"]


def test_parse_none_returns_empty():
    """None → []"""
    from gateway.proxy import _parse_drop_params
    assert _parse_drop_params(None) == []


def test_parse_empty_string_returns_empty():
    """空字符串 → []"""
    from gateway.proxy import _parse_drop_params
    assert _parse_drop_params("") == []
    assert _parse_drop_params("   ") == []


def test_parse_list_input_passthrough():
    """list 输入直接 strip 返回 (ORM 可能已经是 list)"""
    from gateway.proxy import _parse_drop_params
    assert _parse_drop_params(["reasoning_effort", " max_tokens "]) == \
        ["reasoning_effort", "max_tokens"]


def test_parse_invalid_json_falls_back_to_comma():
    """非法 JSON (不以 [ 开头) → 按逗号分隔处理"""
    from gateway.proxy import _parse_drop_params
    assert _parse_drop_params("reasoning_effort") == ["reasoning_effort"]


# ============================================================
# _drop_params — 基本剥离逻辑
# ============================================================

def test_drop_params_basic():
    """基本剥离: drop_params=['reasoning_effort'] → body 里的 reasoning_effort 被删"""
    from gateway.proxy import _drop_params
    body = json.dumps({
        "model": "deepseek-v3",
        "messages": [{"role": "user", "content": "hi"}],
        "reasoning_effort": "high",
        "max_tokens": 1000,
    }).encode("utf-8")
    channel = {"name": "test", "drop_params": '["reasoning_effort"]'}

    new_body, dropped = _drop_params(body, channel)
    assert dropped == ["reasoning_effort"]
    parsed = json.loads(new_body)
    assert "reasoning_effort" not in parsed
    assert "max_tokens" in parsed  # 不在 drop list, 保留


def test_drop_params_multiple():
    """多参数剥离"""
    from gateway.proxy import _drop_params
    body = json.dumps({
        "model": "deepseek-v3",
        "messages": [],
        "reasoning_effort": "high",
        "max_tokens": 1000,
        "temperature": 0.7,
    }).encode("utf-8")
    channel = {"name": "test", "drop_params": "reasoning_effort,max_tokens"}

    new_body, dropped = _drop_params(body, channel)
    assert set(dropped) == {"reasoning_effort", "max_tokens"}
    parsed = json.loads(new_body)
    assert "reasoning_effort" not in parsed
    assert "max_tokens" not in parsed
    assert "temperature" in parsed


# ============================================================
# ALWAYS_KEEP_PARAMS 保护
# ============================================================

def test_drop_params_preserves_messages():
    """messages 永远不被剥离 (即使配在 drop_params 里)"""
    from gateway.proxy import _drop_params
    body = json.dumps({
        "model": "deepseek-v3",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
    }).encode("utf-8")
    channel = {"name": "test", "drop_params": '["messages"]'}

    new_body, dropped = _drop_params(body, channel)
    assert dropped == []  # messages 在 ALWAYS_KEEP_PARAMS 里, 不剥离
    parsed = json.loads(new_body)
    assert "messages" in parsed


def test_drop_params_preserves_model():
    """model 永远不被剥离"""
    from gateway.proxy import _drop_params
    body = json.dumps({
        "model": "deepseek-v3",
        "messages": [],
    }).encode("utf-8")
    channel = {"name": "test", "drop_params": '["model"]'}

    new_body, dropped = _drop_params(body, channel)
    assert dropped == []
    assert json.loads(new_body)["model"] == "deepseek-v3"


def test_drop_params_preserves_stream():
    """stream 永远不被剥离"""
    from gateway.proxy import _drop_params
    body = json.dumps({
        "model": "deepseek-v3",
        "messages": [],
        "stream": True,
    }).encode("utf-8")
    channel = {"name": "test", "drop_params": '["stream"]'}

    new_body, dropped = _drop_params(body, channel)
    assert dropped == []
    assert json.loads(new_body)["stream"] is True


# ============================================================
# channel 无配置 / 早返回
# ============================================================

def test_drop_params_no_config_returns_original():
    """channel 无 drop_params → 原样返回, dropped=[]"""
    from gateway.proxy import _drop_params
    body = b'{"model":"deepseek-v3","messages":[]}'
    channel = {"name": "test"}

    new_body, dropped = _drop_params(body, channel)
    assert dropped == []
    assert new_body == body  # 同一对象


def test_drop_params_empty_config_returns_original():
    """drop_params 为空字符串 → 原样返回"""
    from gateway.proxy import _drop_params
    body = b'{"model":"deepseek-v3","messages":[]}'
    channel = {"name": "test", "drop_params": ""}

    new_body, dropped = _drop_params(body, channel)
    assert dropped == []
    assert new_body == body


def test_drop_params_none_config_returns_original():
    """drop_params 为 None → 原样返回"""
    from gateway.proxy import _drop_params
    body = b'{"model":"deepseek-v3","messages":[]}'
    channel = {"name": "test", "drop_params": None}

    new_body, dropped = _drop_params(body, channel)
    assert dropped == []
    assert new_body == body


def test_drop_params_not_in_body_no_op():
    """drop list 里的参数不在 body 里 → 不触发剥离"""
    from gateway.proxy import _drop_params
    body = json.dumps({
        "model": "deepseek-v3",
        "messages": [],
        "max_tokens": 100,
    }).encode("utf-8")
    channel = {"name": "test", "drop_params": '["reasoning_effort"]'}

    new_body, dropped = _drop_params(body, channel)
    assert dropped == []  # body 里没 reasoning_effort
    assert new_body == body


# ============================================================
# 非 JSON body 安全跳过
# ============================================================

def test_drop_params_non_json_body():
    """非 JSON body → 安全跳过, 返回原始 bytes"""
    from gateway.proxy import _drop_params
    body = b"not a json body"
    channel = {"name": "test", "drop_params": '["reasoning_effort"]'}

    new_body, dropped = _drop_params(body, channel)
    assert dropped == []
    assert new_body == body


# ============================================================
# 集成: thinking strip → drop_params 顺序
# ============================================================

def test_drop_params_after_thinking_strip():
    """模拟 forward_request: thinking 先剥, drop_params 再剥剩余字段

    场景: channel 同时有 disable_thinking_pass_through=1 和 drop_params
    body 里有 thinking + reasoning_effort + max_tokens
    预期: thinking 被 thinking strip 剥, max_tokens 被 drop_params 剥
    """
    from gateway.proxy import _strip_thinking_fields, _drop_params
    body_dict = {
        "model": "deepseek-v3",
        "messages": [],
        "thinking": True,
        "reasoning_effort": "high",
        "max_tokens": 1000,
    }

    # Step 1: thinking strip
    stripped = _strip_thinking_fields(body_dict)
    assert "thinking" not in stripped
    assert "reasoning_effort" not in stripped  # reasoning_effort 也在 THINKING_FIELDS_TO_STRIP
    assert "max_tokens" in stripped

    # Step 2: drop_params
    body = json.dumps(stripped).encode("utf-8")
    channel = {"name": "test", "drop_params": '["max_tokens"]'}
    new_body, dropped = _drop_params(body, channel)
    assert dropped == ["max_tokens"]
    final = json.loads(new_body)
    assert "max_tokens" not in final
    assert "model" in final
    assert "messages" in final
