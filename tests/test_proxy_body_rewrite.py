"""V4.6.4 (2026-08-14) Issue 6 proxy body.model rewrite 测试

覆盖:
- _maybe_rewrite_body_model: 改写 body.model (Issue 6 主修复)
- _maybe_rewrite_body_model: 不改 (无 _actual_model)
- _maybe_rewrite_body_model: 不改 (_actual_model == requested_model)
- _maybe_rewrite_body_model: 不改 (body 不是 JSON)
- _maybe_rewrite_body_model: 已匹配不重复改
"""

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def test_rewrite_body_model_changes_model_field():
    """主修复: body.model 从 glm-5.2 -> zai-org/GLM-5.2"""
    from gateway.proxy import _maybe_rewrite_body_model

    body = json.dumps({
        "model": "glm-5.2",
        "messages": [{"role": "user", "content": "ping"}],
    }).encode("utf-8")
    channel = {"_actual_model": "zai-org/GLM-5.2"}

    new_body = _maybe_rewrite_body_model(body, channel, requested_model="glm-5.2")
    parsed = json.loads(new_body)
    assert parsed["model"] == "zai-org/GLM-5.2", \
        f"应 zai-org/GLM-5.2, 实际 {parsed['model']}"
    assert parsed["messages"] == [{"role": "user", "content": "ping"}]
    print(f"  [OK] body.model 改写: glm-5.2 -> zai-org/GLM-5.2")


def test_rewrite_no_op_when_no_actual_model():
    """没 _actual_model 时不改"""
    from gateway.proxy import _maybe_rewrite_body_model

    body = json.dumps({"model": "glm-5.2"}).encode("utf-8")
    channel = {}  # 无 _actual_model

    new_body = _maybe_rewrite_body_model(body, channel, requested_model="glm-5.2")
    assert new_body == body, "应原样返回 body"
    print(f"  [OK] 无 _actual_model: body 原样返回")


def test_rewrite_no_op_when_already_matching():
    """_actual_model == requested_model 时不改"""
    from gateway.proxy import _maybe_rewrite_body_model

    body = json.dumps({"model": "deepseek-v4-flash"}).encode("utf-8")
    channel = {"_actual_model": "deepseek-v4-flash"}

    new_body = _maybe_rewrite_body_model(body, channel, requested_model="deepseek-v4-flash")
    assert new_body == body, "应原样返回 body"
    print(f"  [OK] _actual_model == requested_model: body 原样返回")


def test_rewrite_no_op_when_body_not_json():
    """body 不是 JSON 时不改"""
    from gateway.proxy import _maybe_rewrite_body_model

    body = b"not a json body"
    channel = {"_actual_model": "zai-org/GLM-5.2"}

    new_body = _maybe_rewrite_body_model(body, channel, requested_model="glm-5.2")
    assert new_body == body, "应原样返回 body (非 JSON)"
    print(f"  [OK] body 非 JSON: 原样返回")


def test_rewrite_no_op_when_body_model_already_target():
    """body.model 已等于 _actual_model 不重复改"""
    from gateway.proxy import _maybe_rewrite_body_model

    body = json.dumps({"model": "zai-org/GLM-5.2"}).encode("utf-8")
    channel = {"_actual_model": "zai-org/GLM-5.2"}

    new_body = _maybe_rewrite_body_model(body, channel, requested_model="glm-5.2")
    assert new_body == body, "应原样返回 body (已是目标值)"
    print(f"  [OK] body.model 已等于 _actual_model: 不重复改")


def test_rewrite_preserves_other_fields():
    """改 body.model 不影响其他字段"""
    from gateway.proxy import _maybe_rewrite_body_model

    body = json.dumps({
        "model": "tier:flagship",  # group request
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 100,
        "stream": False,
        "temperature": 0.7,
    }).encode("utf-8")
    channel = {"_actual_model": "deepseek-v4-pro"}

    new_body = _maybe_rewrite_body_model(body, channel, requested_model="tier:flagship")
    parsed = json.loads(new_body)
    assert parsed["model"] == "deepseek-v4-pro"
    assert parsed["max_tokens"] == 100
    assert parsed["stream"] is False
    assert parsed["temperature"] == 0.7
    assert parsed["messages"] == [{"role": "user", "content": "ping"}]
    print(f"  [OK] 改 body.model 不影响 messages/max_tokens/stream/temperature")


def test_rewrite_handles_unicode():
    """中文 content 正确处理"""
    from gateway.proxy import _maybe_rewrite_body_model

    body = json.dumps({
        "model": "glm-5.2",
        "messages": [{"role": "user", "content": "你好"}],
    }, ensure_ascii=False).encode("utf-8")
    channel = {"_actual_model": "zai-org/GLM-5.2"}

    new_body = _maybe_rewrite_body_model(body, channel, requested_model="glm-5.2")
    parsed = json.loads(new_body)
    assert parsed["model"] == "zai-org/GLM-5.2"
    assert parsed["messages"][0]["content"] == "你好"
    print(f"  [OK] 中文 content: 原样保留")


# ============================================================
# 主入口
# ============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("V4.6.4 Issue 6 proxy body.model rewrite 测试")
    print("=" * 60)

    print("\n=== _maybe_rewrite_body_model ===")
    test_rewrite_body_model_changes_model_field()
    test_rewrite_no_op_when_no_actual_model()
    test_rewrite_no_op_when_already_matching()
    test_rewrite_no_op_when_body_not_json()
    test_rewrite_no_op_when_body_model_already_target()
    test_rewrite_preserves_other_fields()
    test_rewrite_handles_unicode()

    print("\n" + "=" * 60)
    print("ALL TESTS PASSED")
    print("=" * 60)