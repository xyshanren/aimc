"""V6.x (2026-10-23): MiniMax 2013 tool 配对清洗测试

背景: MiniMax 对 tool_call/tool_result 配对严格校验, 三种破坏形态上游直探全部
400 (2013) — 孤儿 / 错位 / 缺失。生产 4 天 67 例 (跨模型混历史)。
修复 = channels.sanitize_tool_history=1 时 proxy 转发前归一化。

测试向量 = 2026-10-23 生产账号直探 MiniMax 上游的实测用例:
- C0 合法相邻 → 200 (清洗器必须原样返回)
- C1 孤儿 id 不匹配 → 400 "tool call id is invalid (2013)"
- C2 错位被 user 隔开 → 400 "tool call result does not follow tool call (2013)"
- C3 并行只回一个 → 400 同上文案 (与生产报错一字不差)
- C4 前面无 tool_call → 400 "tool result's tool id not found (2013)"
- C6/C7 清洗器输出形态 → 200 (直探验证)

跑法:
    pytest tests/test_v6x_tool_history_sanitize.py -v
"""
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from gateway.proxy import _message_skeleton, _sanitize_tool_history, _tool_content_text


def _asst_tool(ids):
    return {"role": "assistant", "content": "",
            "tool_calls": [{"id": i, "type": "function",
                            "function": {"name": "get_weather",
                                          "arguments": json.dumps({"city": "北京"})}}
                           for i in ids]}


def _tool_result(tid, text="晴 25 度"):
    return {"role": "tool", "tool_call_id": tid, "content": text}


def _run(msgs):
    body = json.dumps({"model": "minimax-m3.1-flash", "messages": msgs,
                        "tools": [{"type": "function",
                                    "function": {"name": "get_weather"}}]},
                       ensure_ascii=False).encode("utf-8")
    out = _sanitize_tool_history(body)
    return json.loads(out)["messages"]


# ============================================================
# C0: 干净历史 — 原样返回 (字节级不变)
# ============================================================

def test_clean_history_untouched():
    """C0: 合法相邻配对上游实测 200, 清洗器不得改动 (零拷贝)"""
    body = json.dumps({"model": "m", "messages": [
        {"role": "user", "content": "北京天气如何"},
        _asst_tool(["tc1"]),
        _tool_result("tc1"),
        {"role": "user", "content": "总结"},
    ], "tools": [{"type": "function", "function": {"name": "get_weather"}}]},
        ensure_ascii=False).encode("utf-8")
    assert _sanitize_tool_history(body) == body, "干净历史应原样返回"


def test_parallel_complete_untouched():
    """并行 tool_calls 全部回传且相邻 — 原样返回"""
    body = json.dumps({"messages": [
        {"role": "user", "content": "q"},
        _asst_tool(["a", "b"]),
        _tool_result("a"), _tool_result("b"),
        {"role": "user", "content": "next"},
    ]}).encode()
    assert _sanitize_tool_history(body) == body


# ============================================================
# C1: 孤儿 (id 不匹配) → 转 user 文本 + 缺失补合成结果
# ============================================================

def test_orphan_mismatch_converted():
    """C1: tool_call_id 匹配不到 → 孤儿转 user, 未决 tc1 补合成结果 (C6 形态, 上游 200)"""
    out = _run([
        {"role": "user", "content": "北京天气如何"},
        _asst_tool(["tc1"]),
        _tool_result("tc_OTHER"),
        {"role": "user", "content": "总结"},
    ])
    # 期望: [user, asst(tc1), tool(tc1 合成), user(孤儿转换), user(总结)]
    assert out[1]["role"] == "assistant"
    assert out[2]["role"] == "tool" and out[2]["tool_call_id"] == "tc1"
    assert out[2]["content"] == "[tool result unavailable]"
    assert out[3]["role"] == "user" and "tc_OTHER" in out[3]["content"]
    assert "晴 25 度" in out[3]["content"], "孤儿转换应保留原内容"
    assert out[4]["content"] == "总结"


def test_orphan_no_preceding_call():
    """C4: 前面根本没有 tool_call → 直接转 user"""
    out = _run([
        {"role": "user", "content": "你好"},
        _tool_result("tc1"),
        {"role": "user", "content": "总结"},
    ])
    assert [m["role"] for m in out] == ["user", "user", "user"]
    assert "tc1" in out[1]["content"]


# ============================================================
# C2: 错位 (结果被 user 隔开) → 归位到紧贴 assistant
# ============================================================

def test_displaced_result_repositioned():
    """C2: 结果被 user 消息隔开 → 结果归位 assistant 之后 (C7 形态, 上游 200)"""
    out = _run([
        {"role": "user", "content": "北京天气如何"},
        _asst_tool(["tc1"]),
        {"role": "user", "content": "对了顺便问下上海"},
        _tool_result("tc1"),
        {"role": "user", "content": "总结"},
    ])
    # 期望: [user, asst, tool(tc1 原结果归位), user(隔断消息回放), user(总结)]
    assert out[2]["role"] == "tool" and out[2]["tool_call_id"] == "tc1"
    assert out[2]["content"] == "晴 25 度", "归位的应是原结果 (非合成)"
    assert out[3]["content"] == "对了顺便问下上海"
    assert out[4]["content"] == "总结"


# ============================================================
# C3: 并行缺失 → 未决的补合成结果 (生产报错同文案)
# ============================================================

def test_partial_results_synthesized():
    """C3: 两个调用只回一个 → tc2 补合成结果, 紧跟 tc1 真实结果"""
    out = _run([
        {"role": "user", "content": "北京和上海天气"},
        _asst_tool(["tc1", "tc2"]),
        _tool_result("tc1"),
        {"role": "user", "content": "总结"},
    ])
    assert out[2]["tool_call_id"] == "tc1" and out[2]["content"] == "晴 25 度"
    assert out[3]["role"] == "tool" and out[3]["tool_call_id"] == "tc2"
    assert out[3]["content"] == "[tool result unavailable]"
    assert out[4]["content"] == "总结"


def test_zero_results_synthesized():
    """assistant(tool_calls) 后完全没有结果 → 全部补合成 (防"零结果"变体)"""
    out = _run([
        {"role": "user", "content": "q"},
        _asst_tool(["tc1"]),
        {"role": "user", "content": "总结"},
    ])
    assert out[2]["role"] == "tool" and out[2]["tool_call_id"] == "tc1"
    assert out[3]["content"] == "总结"


def test_empty_assistant_content_filled():
    """ch1 生产 3 例同族: content 与 tool_calls 双缺的 assistant → 补 content=""

    DeepSeek 官方 400 "Invalid assistant message: content or tool_calls must be
    set"; 清洗后形态直探 DeepSeek 实测 200 (2026-10-27)。reasoning_content 保留。
    """
    out = _run([
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": None, "reasoning_content": "思考过"},
        {"role": "user", "content": "总结"},
    ])
    assert out[1]["role"] == "assistant"
    assert out[1]["content"] == ""
    assert out[1]["reasoning_content"] == "思考过"


def test_tool_calls_assistant_null_content_filled():
    """ModelScope 生产 2026-10-29: 带 tool_calls 的 assistant content=None 也拒收

    ModelScope Pydantic 严格校验 "20 validation errors ... 'content', 'str'
    ... 'input': None" (生产 33 例, message_skeleton 取证形态 `a~tc[...]~none`)。
    补 content="" 后直探 DeepSeek/MiniMax 实测 200 (2026-10-29)。
    """
    out = _run([
        {"role": "user", "content": "北京天气如何"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "tc1", "type": "function",
                         "function": {"name": "get_weather", "arguments": "{}"}}]},
        _tool_result("tc1"),
    ])
    assert out[1]["content"] == ""
    assert out[1]["tool_calls"][0]["id"] == "tc1"
    assert out[2]["role"] == "tool" and out[2]["tool_call_id"] == "tc1"


def test_tool_calls_assistant_missing_content_key_filled():
    """content 键整个缺失且带 tool_calls → 同样补空串"""
    out = _run([
        {"role": "user", "content": "q"},
        {"role": "assistant",
         "tool_calls": [{"id": "tc1", "type": "function",
                         "function": {"name": "get_weather", "arguments": "{}"}}]},
        _tool_result("tc1"),
    ])
    assert out[1]["content"] == ""


def test_tool_calls_assistant_with_content_untouched():
    """带 tool_calls 且 content 已是字符串 → 原样保留 (零改写)"""
    out = _run([
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "我来查一下",
         "tool_calls": [{"id": "tc1", "type": "function",
                         "function": {"name": "get_weather", "arguments": "{}"}}]},
        _tool_result("tc1"),
    ])
    assert out[1]["content"] == "我来查一下"


def test_assistant_missing_content_key_filled():
    """content 键整个缺失的 assistant → 同样补空串"""
    out = _run([
        {"role": "user", "content": "你好"},
        {"role": "assistant", "reasoning_content": "x"},
        {"role": "user", "content": "总结"},
    ])
    assert out[1]["content"] == ""


def test_assistant_with_content_untouched():
    """有 content 的普通 assistant 不动 (零改写)"""
    body = json.dumps({"messages": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "再总结"},
    ]}).encode()
    assert _sanitize_tool_history(body) == body


def test_missing_results_synthesized_ch1_family():
    """ch1 生产 654 例同族: assistant(tool_calls) 缺失结果 → 补合成 (DeepSeek 探 200)"""
    out = _run([
        {"role": "user", "content": "北京天气如何"},
        _asst_tool(["tc1"]),
        {"role": "user", "content": "总结"},
    ])
    assert out[2]["role"] == "tool" and out[2]["tool_call_id"] == "tc1"
    assert out[2]["content"] == "[tool result unavailable]"
    assert out[3]["content"] == "总结"


# ============================================================
# 边界: 多轮 assistant / 非 tool 流量 / 非法 body
# ============================================================

def test_multi_turn_pairs_preserved():
    """多轮 tool 往返: 每轮各自配对完整 — 原样返回"""
    body = json.dumps({"messages": [
        {"role": "user", "content": "q1"},
        _asst_tool(["t1"]), _tool_result("t1"),
        {"role": "assistant", "content": "中间总结"},
        _asst_tool(["t2"]), _tool_result("t2"),
        {"role": "user", "content": "q2"},
    ]}).encode()
    assert _sanitize_tool_history(body) == body


def test_no_tool_messages_untouched():
    """无 assistant(tool_calls) 的普通会话 — 原样返回"""
    body = json.dumps({"messages": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]}).encode()
    assert _sanitize_tool_history(body) == body


def test_non_json_body_untouched():
    """非 JSON body — 原样返回 (解析失败兜底)"""
    assert _sanitize_tool_history(b"not-json") == b"not-json"


def test_empty_messages_untouched():
    body = json.dumps({"messages": []}).encode()
    assert _sanitize_tool_history(body) == body


def test_tool_content_text_variants():
    """孤儿转换时 content 形态归一: str / list[text] / list[非文本]"""
    assert _tool_content_text("abc") == "abc"
    assert _tool_content_text(None) == ""
    assert _tool_content_text([{"type": "text", "text": "a"},
                                {"type": "image_url"}]) == "a {\"type\": \"image_url\"}"


# ============================================================
# 挂点: proxy.py 源码校验 (渠道标记门控, 同 reasoning echo 模式)
# ============================================================

def test_proxy_hook_gated_by_channel_flag():
    """proxy 源码: sanitize_tool_history 渠道标记门控 + 挂在 reasoning echo 之后"""
    import gateway.proxy as proxy_mod
    import inspect
    src = inspect.getsource(proxy_mod)
    assert 'channel.get("sanitize_tool_history")' in src, \
        "应有渠道标记门控"
    assert src.index('channel.get("sanitize_tool_history")') > \
        src.index('channel.get("requires_reasoning_echo")'), \
        "清洗钩子应在 reasoning echo 之后"


def test_router_channel_dict_carries_flag():
    """router 源码: 渠道 dict 携带 sanitize_tool_history (否则 proxy 拿不到)"""
    import gateway.router as router_mod
    import inspect
    src = inspect.getsource(router_mod)
    assert '"sanitize_tool_history": ch.sanitize_tool_history' in src


# ============================================================
# message_skeleton (非 200 transcript 取证)
# ============================================================

def test_message_skeleton_basic():
    body = json.dumps({"tools": [{"type": "function"}], "messages": [
        {"role": "user", "content": "hi"},
        _asst_tool(["tc1", "tc2"]),
        _tool_result("tc1"),
        {"role": "user", "content": [
            {"type": "text", "text": "图"},
            {"type": "image_url", "image_url": {"url": "data:..."}}]},
    ]}).encode()
    sk = _message_skeleton(body)
    assert sk.startswith("tools=1|")
    assert "user~str" in sk
    assert "a~tc[tc1,tc2]" in sk
    assert "t~tc1~str" in sk
    assert "user~list[text,image_url]" in sk


def test_message_skeleton_truncated_body_returns_empty():
    """截断的 body 解析失败 → 空骨架 (不抛异常)"""
    assert _message_skeleton(b'{"messages": [{"role": "user", "content": "abc') == ""


def test_skeleton_capped():
    """骨架超限截断到 2000 字符"""
    body = json.dumps({"messages": [
        {"role": "user", "content": "x" * 100}] * 200}).encode()
    assert len(_message_skeleton(body)) <= 2000


def main():
    tests = [
        ("C0 干净历史原样返回", test_clean_history_untouched),
        ("并行完整原样返回", test_parallel_complete_untouched),
        ("C1 孤儿转 user+补位", test_orphan_mismatch_converted),
        ("C4 无前置调用孤儿转 user", test_orphan_no_preceding_call),
        ("C2 错位结果归位", test_displaced_result_repositioned),
        ("C3 并行缺失补合成", test_partial_results_synthesized),
        ("零结果全补合成", test_zero_results_synthesized),
        ("多轮配对完整原样返回", test_multi_turn_pairs_preserved),
        ("普通会话原样返回", test_no_tool_messages_untouched),
        ("非 JSON body 原样返回", test_non_json_body_untouched),
        ("空 messages 原样返回", test_empty_messages_untouched),
        ("content 形态归一", test_tool_content_text_variants),
        ("proxy 钩子渠道门控", test_proxy_hook_gated_by_channel_flag),
        ("router 渠道 dict 带标记", test_router_channel_dict_carries_flag),
        ("骨架基本形态", test_message_skeleton_basic),
        ("骨架截断 body 空串", test_message_skeleton_truncated_body_returns_empty),
        ("骨架超限截断", test_skeleton_capped),
    ]
    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  [OK] {name}")
            passed += 1
        except AssertionError as e:
            print(f"  [FAIL] {name}: {e}")
            failed += 1
        except Exception as e:
            print(f"  [ERROR] {name}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\nResult: {passed} passed, {failed} failed, {len(tests)} total")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
