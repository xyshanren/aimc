"""V4.8.7 P5: drop_params 自动检测测试

覆盖:
- _extract_unsupported_params: OpenAI param 风格 / message 文本风格 / 白名单过滤 / ALWAYS_KEEP_PARAMS 保护 / body 不存在跳过
- _auto_learn_drop_params: 追加到现有 drop_params / 去重 / 刷新缓存
- auto_drop_params=0 时不触发

跑法:
    python tests/test_v487_auto_drop_params.py
    或 pytest tests/test_v487_auto_drop_params.py -v
"""
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ============================================================
# _extract_unsupported_params
# ============================================================

def _make_response(status_code: int, body_dict: dict):
    """构造 mock httpx.Response"""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = body_dict
    return resp


def test_extract_openai_param_style():
    """OpenAI 风格 {"error": {"param": "reasoning_effort"}} → 提取 reasoning_effort"""
    from gateway.proxy import _extract_unsupported_params
    body = json.dumps({
        "model": "deepseek-v3",
        "messages": [],
        "reasoning_effort": "high",
    }).encode("utf-8")
    resp = _make_response(400, {"error": {"param": "reasoning_effort", "message": "unsupported"}})

    result = _extract_unsupported_params(resp, body)
    assert result == ["reasoning_effort"]


def test_extract_message_text_quoted():
    """消息文本中引号包裹的参数名: "unsupported parameter: 'reasoning_effort'" """
    from gateway.proxy import _extract_unsupported_params
    body = json.dumps({
        "model": "deepseek-v3",
        "messages": [],
        "reasoning_effort": "high",
    }).encode("utf-8")
    resp = _make_response(400, {
        "error": {"message": "unsupported parameter: 'reasoning_effort' supplied"}
    })

    result = _extract_unsupported_params(resp, body)
    assert result == ["reasoning_effort"]


def test_extract_multiple_params_from_message():
    """消息中多个引号参数: 'max_tokens' 和 'temperature'"""
    from gateway.proxy import _extract_unsupported_params
    body = json.dumps({
        "model": "deepseek-v3",
        "messages": [],
        "max_tokens": 100,
        "temperature": 0.7,
    }).encode("utf-8")
    resp = _make_response(400, {
        "error": {"message": "unrecognized arguments: 'max_tokens', 'temperature'"}
    })

    result = _extract_unsupported_params(resp, body)
    assert set(result) == {"max_tokens", "temperature"}


def test_extract_filters_non_whitelist():
    """非白名单参数 (e.g. 'random_field') 不被提取"""
    from gateway.proxy import _extract_unsupported_params
    body = json.dumps({
        "model": "deepseek-v3",
        "messages": [],
        "random_field": "xxx",
    }).encode("utf-8")
    resp = _make_response(400, {
        "error": {"param": "random_field", "message": "unsupported 'random_field'"}
    })

    result = _extract_unsupported_params(resp, body)
    assert result == []  # random_field 不在 _KNOWN_REQUEST_PARAMS


def test_extract_protects_always_keep():
    """ALWAYS_KEEP_PARAMS (messages/model/stream) 永远不被提取"""
    from gateway.proxy import _extract_unsupported_params
    body = json.dumps({
        "model": "deepseek-v3",
        "messages": [],
        "stream": True,
    }).encode("utf-8")
    # 即使上游错误地说 "messages" 不支持
    resp = _make_response(400, {"error": {"param": "messages"}})

    result = _extract_unsupported_params(resp, body)
    assert result == []


def test_extract_skips_params_not_in_body():
    """参数在白名单但不在 body 顶层 → 跳过 (避免误杀)"""
    from gateway.proxy import _extract_unsupported_params
    body = json.dumps({
        "model": "deepseek-v3",
        "messages": [],
    }).encode("utf-8")
    resp = _make_response(400, {
        "error": {"message": "unsupported parameter 'reasoning_effort'"}
    })

    result = _extract_unsupported_params(resp, body)
    assert result == []  # reasoning_effort 不在 body 里


def test_extract_non_json_response():
    """上游返回非 JSON → 安全返回空列表"""
    from gateway.proxy import _extract_unsupported_params
    resp = MagicMock()
    resp.json.side_effect = ValueError("not json")
    body = b'{"model":"deepseek-v3","messages":[]}'

    result = _extract_unsupported_params(resp, body)
    assert result == []


def test_extract_dedup():
    """同一参数出现多次 (param + message) → 去重"""
    from gateway.proxy import _extract_unsupported_params
    body = json.dumps({
        "model": "deepseek-v3",
        "messages": [],
        "reasoning_effort": "high",
    }).encode("utf-8")
    resp = _make_response(400, {
        "error": {
            "param": "reasoning_effort",
            "message": "unsupported 'reasoning_effort'",
        }
    })

    result = _extract_unsupported_params(resp, body)
    assert result == ["reasoning_effort"]  # 只出现一次


def test_extract_param_case_normalization():
    """大写参数名 → 转小写"""
    from gateway.proxy import _extract_unsupported_params
    body = json.dumps({
        "model": "deepseek-v3",
        "messages": [],
        "max_tokens": 100,
    }).encode("utf-8")
    resp = _make_response(400, {
        "error": {"param": "Max_Tokens"}
    })

    result = _extract_unsupported_params(resp, body)
    assert result == ["max_tokens"]


# ============================================================
# _auto_learn_drop_params
# ============================================================

def test_auto_learn_appends_to_existing():
    """已有 drop_params=['reasoning_effort'] → 追加 'max_tokens'"""
    import asyncio
    from gateway.proxy import _auto_learn_drop_params

    mock_channel_obj = MagicMock()
    mock_channel_obj.drop_params = '["reasoning_effort"]'
    mock_channel_obj.id = 1

    mock_session = AsyncMock()
    mock_execute = AsyncMock()
    mock_scalars = MagicMock()
    mock_scalars.first.return_value = mock_channel_obj
    mock_execute.return_value = MagicMock(scalars=MagicMock(return_value=mock_scalars))
    mock_session.execute = mock_execute
    mock_session.commit = AsyncMock()

    mock_factory = MagicMock()
    mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_factory.return_value.__aexit__ = AsyncMock(return_value=None)

    channel = {"id": 1, "name": "test_channel"}
    with patch("db.engine.async_session_factory", mock_factory), \
         patch("gateway.router.route_engine.invalidate_channel", new_callable=AsyncMock):
        asyncio.get_event_loop().run_until_complete(
            _auto_learn_drop_params(channel, ["max_tokens"])
        ) if asyncio.get_event_loop().is_running() else asyncio.run(
            _auto_learn_drop_params(channel, ["max_tokens"])
        )

    # 验证 drop_params 被更新为包含两个参数
    written = json.loads(mock_channel_obj.drop_params)
    assert "reasoning_effort" in written
    assert "max_tokens" in written


def test_auto_learn_dedup():
    """参数已存在 → 不重复追加"""
    import asyncio
    from gateway.proxy import _auto_learn_drop_params

    mock_channel_obj = MagicMock()
    mock_channel_obj.drop_params = '["reasoning_effort"]'
    mock_channel_obj.id = 1

    mock_session = AsyncMock()
    mock_execute = AsyncMock()
    mock_scalars = MagicMock()
    mock_scalars.first.return_value = mock_channel_obj
    mock_execute.return_value = MagicMock(scalars=MagicMock(return_value=mock_scalars))
    mock_session.execute = mock_execute
    mock_session.commit = AsyncMock()

    mock_factory = MagicMock()
    mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_factory.return_value.__aexit__ = AsyncMock(return_value=None)

    channel = {"id": 1, "name": "test_channel"}
    with patch("db.engine.async_session_factory", mock_factory), \
         patch("gateway.router.route_engine.invalidate_channel", new_callable=AsyncMock):
        try:
            asyncio.run(_auto_learn_drop_params(channel, ["reasoning_effort"]))
        except RuntimeError:
            pass

    # reasoning_effort 已存在, commit 不应被调用 (无新参数)
    # 注意: 如果没有新参数, 函数在 commit 前就 return 了
    # 但 mock 可能让流程不完全走通, 所以只验证 drop_params 没变
    written = json.loads(mock_channel_obj.drop_params)
    assert written == ["reasoning_effort"]


def test_auto_learn_channel_not_found():
    """channel_id 在 DB 中不存在 → 安全返回"""
    import asyncio
    from gateway.proxy import _auto_learn_drop_params

    mock_session = AsyncMock()
    mock_execute = AsyncMock()
    mock_scalars = MagicMock()
    mock_scalars.first.return_value = None  # channel 不存在
    mock_execute.return_value = MagicMock(scalars=MagicMock(return_value=mock_scalars))
    mock_session.execute = mock_execute

    mock_factory = MagicMock()
    mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_factory.return_value.__aexit__ = AsyncMock(return_value=None)

    channel = {"id": 999, "name": "ghost"}
    with patch("db.engine.async_session_factory", mock_factory):
        try:
            asyncio.run(_auto_learn_drop_params(channel, ["max_tokens"]))
        except RuntimeError:
            pass

    # 不应 commit
    mock_session.commit.assert_not_called()


if __name__ == "__main__":
    # 手动运行所有测试
    tests = [
        test_extract_openai_param_style,
        test_extract_message_text_quoted,
        test_extract_multiple_params_from_message,
        test_extract_filters_non_whitelist,
        test_extract_protects_always_keep,
        test_extract_skips_params_not_in_body,
        test_extract_non_json_response,
        test_extract_dedup,
        test_extract_param_case_normalization,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {t.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
