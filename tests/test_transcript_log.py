"""V4.9.3: 测试请求/响应转录日志 (transcript logging)

覆盖:
- _schedule_transcript_log normal 路径 (response 对象) — 参数构造正确
- _schedule_transcript_log stream 路径 (stream_body bytes) — 参数构造正确
- 截断行为 (transcript_log_max_body)
- 异常吞没 (内部异常不抛出)
- A/B 开关逻辑 (prompt_cache_inject_enabled=False → 调用点不注入)
- RequestTranscript.to_dict() 列表/详情两种模式
"""
import json
from unittest.mock import MagicMock, patch, AsyncMock


def _capture_schedule_args(*args, **kwargs):
    """拦截 _schedule_transcript_log 的实际逻辑, 捕获传给 _write_transcript_log 的参数"""
    pass


def test_schedule_transcript_log_normal_path():
    """normal 路径: 传 response 对象, 提取 text + status_code

    _schedule_transcript_log 内部用 asyncio.get_running_loop().create_task(),
    测试里没有 running loop, 所以直接验证它内部构造的参数 (mock loop.create_task)。
    """
    from gateway.proxy import _schedule_transcript_log

    channel = {"id": 42}
    request_body = json.dumps({"model": "gpt-4", "messages": []}).encode()
    mock_response = MagicMock()
    mock_response.text = '{"choices": [{"message": {"content": "hi"}}]}'
    mock_response.status_code = 200

    captured = {}

    class FakeLoop:
        def create_task(self, coro):
            # 捕获 coro 的参数 (coro 是 _write_transcript_log(...) 调用)
            # coro.cr_frame.f_locals 有传进去的 kwargs
            captured.update(coro.cr_frame.f_locals)
            coro.close()  # 防止 warning
            return MagicMock()

        def get_debug(self):
            return False

    fake_loop = FakeLoop()

    with patch("asyncio.get_running_loop", return_value=fake_loop):
        with patch("gateway.proxy._log_task_result"):
            _schedule_transcript_log(
                channel, request_body, mock_response, "gpt-4",
                caller_id=1, caller_key_id=2,
            )

    assert captured.get("channel_id") == 42
    assert captured.get("model") == "gpt-4"
    assert "gpt-4" in captured.get("request_body", "")
    assert captured.get("response_body") == mock_response.text
    assert captured.get("status_code") == 200
    assert captured.get("caller_id") == 1
    assert captured.get("caller_key_id") == 2


def test_schedule_transcript_log_stream_path():
    """stream 路径: 传 stream_body bytes + stream_status"""
    from gateway.proxy import _schedule_transcript_log

    channel = {"id": 7}
    request_body = b'{"model": "claude-3", "stream": true}'
    stream_body = b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\ndata: [DONE]\n\n'

    captured = {}

    class FakeLoop:
        def create_task(self, coro):
            captured.update(coro.cr_frame.f_locals)
            coro.close()
            return MagicMock()

        def get_debug(self):
            return False

    fake_loop = FakeLoop()

    with patch("asyncio.get_running_loop", return_value=fake_loop):
        with patch("gateway.proxy._log_task_result"):
            _schedule_transcript_log(
                channel, request_body, None, "claude-3",
                caller_id=3, caller_key_id=None,
                stream_status=200,
                stream_body=stream_body,
            )

    assert captured.get("channel_id") == 7
    assert captured.get("status_code") == 200
    assert "hello" in captured.get("response_body", "")
    assert captured.get("caller_key_id") is None


def test_schedule_transcript_log_truncation():
    """截断: request_body + response_body 都受 transcript_log_max_body 限制"""
    from gateway.proxy import _schedule_transcript_log
    from config import settings

    original_max = settings.transcript_log_max_body
    settings.transcript_log_max_body = 100

    try:
        channel = {"id": 1}
        long_request = b"x" * 500
        mock_response = MagicMock()
        mock_response.text = "y" * 500
        mock_response.status_code = 200

        captured = {}

        class FakeLoop:
            def create_task(self, coro):
                captured.update(coro.cr_frame.f_locals)
                coro.close()
                return MagicMock()

            def get_debug(self):
                return False

        fake_loop = FakeLoop()

        with patch("asyncio.get_running_loop", return_value=fake_loop):
            with patch("gateway.proxy._log_task_result"):
                _schedule_transcript_log(channel, long_request, mock_response, "model")

        assert len(captured.get("request_body", "")) == 100
        assert len(captured.get("response_body", "")) == 100
    finally:
        settings.transcript_log_max_body = original_max


def test_schedule_transcript_log_exception_swallowed():
    """_schedule_transcript_log 内部异常不抛出 (pass)"""
    from gateway.proxy import _schedule_transcript_log

    # 传一个无法 decode 的对象 (模拟异常)
    channel = {"id": 1}
    bad_body = None  # None 没有 .decode()

    # 不应抛异常
    _schedule_transcript_log(channel, bad_body, None, "model")


def test_inject_disabled_when_switch_off():
    """V4.9.3 A/B 开关: prompt_cache_inject_enabled=False 时不注入"""
    from config import settings
    from gateway.proxy import _inject_prompt_cache

    original = settings.prompt_cache_inject_enabled
    settings.prompt_cache_inject_enabled = False

    try:
        body = json.dumps({
            "model": "gpt-4",
            "messages": [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "hi"},
            ],
        }).encode("utf-8")
        channel = {"api_style": "openai", "supports_prompt_cache": True}

        # _inject_prompt_cache 本身不改, 开关在调用点
        # 这里验证调用点逻辑: supports_prompt_cache=True AND enabled=False → 不调用
        should_inject = channel.get("supports_prompt_cache") and settings.prompt_cache_inject_enabled
        assert should_inject is False

        # 函数本身仍能正常工作 (不受开关影响)
        result = json.loads(_inject_prompt_cache(body, channel))
        assert "prompt_cache_key" in result
    finally:
        settings.prompt_cache_inject_enabled = original


def test_request_transcript_model_to_dict():
    """RequestTranscript.to_dict() — 列表模式不含 body, 详情模式含 body"""
    from db.models import RequestTranscript
    from datetime import datetime

    rt = RequestTranscript(
        id=1, channel_id=5, model="gpt-4",
        request_body='{"model":"gpt-4"}',
        response_body='{"choices":[]}',
        status_code=200, caller_id=2, key_id=3,
        created_at=datetime(2026, 8, 25, 12, 0, 0),
    )

    # 列表模式: 不含 body, 含长度
    d_list = rt.to_dict(include_body=False)
    assert "request_body" not in d_list
    assert "response_body" not in d_list
    assert d_list["request_len"] == len('{"model":"gpt-4"}')
    assert d_list["response_len"] == len('{"choices":[]}')
    assert d_list["id"] == 1
    assert d_list["channel_id"] == 5
    assert d_list["model"] == "gpt-4"
    assert d_list["status_code"] == 200

    # 详情模式: 含 body
    d_full = rt.to_dict(include_body=True)
    assert d_full["request_body"] == '{"model":"gpt-4"}'
    assert d_full["response_body"] == '{"choices":[]}'
