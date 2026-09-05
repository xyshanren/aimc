"""V4.6.4+: 测试 proxy 剥离 thinking 相关字段 (channel.disable_thinking_pass_through)

修复前: 请求带 thinking={...} 或 enable_thinking=true 透传到上游,
        minimax-m2.7 等输出 1000+ token thinking 块, agent loop 慢.
修复后: proxy 层主动剥离这些字段, 上游不输出 thinking.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(ROOT))

import json

from gateway.proxy import _parse_reasoning_tokens, _strip_thinking_fields


class TestStripThinking:
    """测试剥离逻辑 (从 proxy._strip_thinking_fields 提取)"""

    def test_strips_thinking(self):
        body = {"model": "m", "thinking": {"type": "enabled", "budget_tokens": 1000}, "messages": []}
        out = _strip_thinking_fields(body)
        assert "thinking" not in out
        assert "model" in out

    def test_strips_enable_thinking(self):
        body = {"model": "m", "enable_thinking": True}
        out = _strip_thinking_fields(body)
        assert "enable_thinking" not in out

    def test_strips_reasoning_effort(self):
        body = {"model": "m", "reasoning_effort": "low"}
        out = _strip_thinking_fields(body)
        assert "reasoning_effort" not in out

    def test_strips_extended_thinking(self):
        body = {"model": "m", "extended_thinking": True}
        out = _strip_thinking_fields(body)
        assert "extended_thinking" not in out

    def test_strips_all_four(self):
        body = {
            "model": "m",
            "thinking": {"type": "enabled"},
            "enable_thinking": True,
            "reasoning_effort": "high",
            "extended_thinking": {"budget": 2000},
        }
        out = _strip_thinking_fields(body)
        assert "thinking" not in out
        assert "enable_thinking" not in out
        assert "reasoning_effort" not in out
        assert "extended_thinking" not in out

    def test_no_thinking_fields_no_op(self):
        body = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
        out = _strip_thinking_fields(body)
        assert out == body
        # 无 thinking 字段时, 函数返回原 dict (优化: 避免无谓 copy)
        assert out is body  # 优化路径, 同一对象引用

    def test_preserves_other_fields(self):
        body = {"model": "m", "messages": [], "temperature": 0.7, "max_tokens": 100}
        out = _strip_thinking_fields(body)
        assert out["temperature"] == 0.7
        assert out["max_tokens"] == 100

    def test_does_not_modify_original(self):
        """strip 应该是 immutable (返回新 dict)"""
        body = {"model": "m", "thinking": {"budget": 1000}, "temperature": 0.5}
        _ = _strip_thinking_fields(body)
        assert "thinking" in body  # 原 dict 不变
        assert body["temperature"] == 0.5

    def test_empty_body(self):
        out = _strip_thinking_fields({})
        assert out == {}

    def test_messages_with_thinking_inside(self):
        """thinking 在 messages 内部 (不剥离, 只剥离顶层字段)"""
        body = {
            "model": "m",
            "messages": [{"role": "user", "content": "hi", "thinking": "old"}],
            "thinking": {"type": "enabled"},  # 顶层, 剥离
        }
        out = _strip_thinking_fields(body)
        assert "thinking" not in out
        # messages 内部的 thinking 保留 (有些 agent 把 reasoning 放在 message)
        assert out["messages"][0]["thinking"] == "old"


class TestParseReasoningTokens:
    """V6.x: 思考 token 解析 — usage.completion_tokens_details.reasoning_tokens"""

    def test_openai_style(self):
        usage = {"completion_tokens_details": {"reasoning_tokens": 123}}
        assert _parse_reasoning_tokens(usage) == 123

    def test_openai_style_zero(self):
        usage = {"completion_tokens_details": {"reasoning_tokens": 0}}
        assert _parse_reasoning_tokens(usage) == 0

    def test_no_details(self):
        assert _parse_reasoning_tokens({"completion_tokens": 50}) is None

    def test_none_usage(self):
        assert _parse_reasoning_tokens(None) is None

    def test_empty_usage(self):
        assert _parse_reasoning_tokens({}) is None

    def test_non_int_value(self):
        # 上游返回异常类型 (str/None) → 不计
        assert _parse_reasoning_tokens(
            {"completion_tokens_details": {"reasoning_tokens": "abc"}}
        ) is None
        assert _parse_reasoning_tokens(
            {"completion_tokens_details": {"reasoning_tokens": None}}
        ) is None

    def test_string_value_rejected(self):
        # 严格解析: 非 int 类型 (含数字字符串) 不计, 与 cache token 解析风格一致
        assert _parse_reasoning_tokens(
            {"completion_tokens_details": {"reasoning_tokens": "42"}}
        ) is None

    def test_non_dict_usage(self):
        assert _parse_reasoning_tokens("not-a-dict") is None
        assert _parse_reasoning_tokens(123) is None