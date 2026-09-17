#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V6.x 回归测试: _forward_stream 双重消费 (httpx.StreamConsumed)

问题: _forward_stream 在 status>=400 时先 await response.aread() (V4.9.1 修复,
为了 classify/alert 能读错误体), 之后 stream_generator 仍调 response.aiter_raw()
— 流已消费, httpx 抛 StreamConsumed, 客户端只收到错误 status 而 body 为空,
tail/full 缓冲也是空的 (错误体完全丢失)。

生产证据: ubuntu2401 journalctl 9/5~9/9 共 326 次 StreamConsumed, 均落在
proxy.py aiter_raw 行。

修复: aread 后检测 response.is_stream_consumed, generator 对已消费流直接
yield 缓冲字节 (tail/full 缓冲语义与正常路径一致)。

用法: python tests/test_v6x_stream_consumed.py  或 pytest tests/test_v6x_stream_consumed.py
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import asyncio
import unittest.mock as mock

import httpx

import gateway.proxy as proxy

_passed = 0
_failed = 0


def check(desc, got, expected):
    global _passed, _failed
    if got == expected:
        _passed += 1
        print(f"  [PASS] {desc}")
    else:
        _failed += 1
        print(f"  [FAIL] {desc}: got={got!r} expected={expected!r}")


class FakeHeaders(dict):
    """dict 兼容 httpx.Headers 的最小接口"""

    def __init__(self, pairs):
        super().__init__(pairs)


class FakeResponse:
    """模拟 httpx 流式响应。status>=400 时 aread() 缓冲 body,
    之后 aiter_raw() 抛 StreamConsumed — 与真实 httpx 行为一致。"""

    def __init__(self, status_code, body, headers=None):
        self.status_code = status_code
        self._body = body
        self.headers = FakeHeaders(headers or {"content-type": "application/json"})
        self.content = b""
        self.is_stream_consumed = False
        self._aread_called = False

    async def aread(self):
        self._aread_called = True
        self.content = self._body
        self.is_stream_consumed = True

    async def aiter_raw(self):
        if self.is_stream_consumed:
            raise httpx.StreamConsumed()
        # 正常路径: 单 chunk 吐出
        yield self._body

    async def aclose(self):
        pass


class FakeClient:
    def __init__(self, response):
        self._response = response
        self.closed = False

    def build_request(self, **kwargs):
        return object()

    async def send(self, req, stream=True):
        return self._response

    async def aclose(self):
        self.closed = True


async def _run_forward(fake_response, channel):
    fake_client = FakeClient(fake_response)
    captured = {}
    orig_usage_log = proxy._schedule_stream_usage_log

    def spy_usage_log(channel, status, latency, model, tail, cid, kid):
        captured["tail"] = tail
        captured["status"] = status

    proxy._schedule_stream_usage_log = spy_usage_log
    orig_cooldown = proxy._update_cooldown
    proxy._update_cooldown = lambda *a, **kw: None
    try:
        with mock.patch.object(httpx, "AsyncClient", lambda *a, **kw: fake_client):
            resp = await proxy._forward_stream(
                "http://upstream.test/v1/chat/completions",
                {"content-type": "application/json"},
                b'{"model":"x"}',
                channel,
                start_time=0.0,
                requested_model="test-model",
            )
        # V6.7: 错误体 (status>=400) 改为缓冲普通 Response 直返 (流式 failover
        # 前置条件); 成功流仍是 StreamingResponse。两种形态都读出完整 body。
        chunks = []
        if hasattr(resp, "body_iterator"):
            async for chunk in resp.body_iterator:
                chunks.append(chunk)
        else:
            chunks = [resp.body]
        return resp, chunks, captured
    finally:
        proxy._schedule_stream_usage_log = orig_usage_log
        proxy._update_cooldown = orig_cooldown


def test_error_stream_delivers_body_after_aread():
    """核心回归: 400 + aread 消费后, 客户端仍能收到完整错误体, 不抛 StreamConsumed"""
    print("[TEST] error stream (status=429) delivers buffered body, no StreamConsumed")
    error_body = b'{"error":{"message":"rate limited","type":"rate_limit_error"}}'
    fake_response = FakeResponse(429, error_body)
    channel = {"name": "test-ch", "id": 1}

    resp, chunks, captured = asyncio.run(_run_forward(fake_response, channel))

    delivered = b"".join(chunks)
    check("响应 status_code 透传", resp.status_code, 429)
    check("客户端收到完整错误体", delivered, error_body)
    check("aread 被调用", fake_response._aread_called, True)
    check("usage log 收到含错误体的 tail", captured.get("tail"), error_body)
    check("usage log 收到 status", captured.get("status"), 429)


def test_success_stream_unchanged():
    """正常路径 (status<400, 流未消费) 仍走 aiter_raw, 行为不变"""
    print("[TEST] success stream (status=200) unchanged via aiter_raw")
    ok_body = b'data: {"choices":[]}\n\ndata: [DONE]\n\n'
    fake_response = FakeResponse(200, ok_body)
    channel = {"name": "test-ch", "id": 1}

    resp, chunks, captured = asyncio.run(_run_forward(fake_response, channel))

    delivered = b"".join(chunks)
    check("响应 status_code 200", resp.status_code, 200)
    check("客户端收到完整流", delivered, ok_body)
    check("aread 未被调用", fake_response._aread_called, False)


def main():
    print("=" * 60)
    print("V6.x StreamConsumed 回归测试")
    print("=" * 60)
    test_error_stream_delivers_body_after_aread()
    test_success_stream_unchanged()
    print("=" * 60)
    print(f"通过 {_passed} / 失败 {_failed}")
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
