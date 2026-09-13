"""V6.5 批次一: S1 失败链响应 Content-Length 损坏回归测试.

背景 (production-review-2026-09-13.md S1, 本机实证):
- routes 最终失败分支复制原响应 headers (含 content-length), 注入
  failed_channels 后 body 变长, 而 starlette init_headers 检测到已有
  content-length 不再重算 → 声明 25B / 实际 351B, h11 断连或客户端截断。
- 修复: 构造新 JSONResponse 前 pop 掉 content-length / transfer-encoding /
  connection。

打真实 ASGI app (不触发 lifespan), mock router/quota 边界。

注意: conftest 的 autouse fixture 在每个测试后清除所有项目模块, 因此被
patch 的目标 (gateway.routes / config.settings) 必须在 fixture 内 import,
取 sys.modules 里的当前份 — 顶层 import 会拿到跨测试失联的旧模块对象。
"""

import json

import httpx
import pytest
from fastapi.responses import JSONResponse


@pytest.fixture
def env(monkeypatch):
    """每测试独立: 重建 app + patch 当前模块实例的边界。"""
    from gateway.app import create_app
    import gateway.routes as routes_mod
    import gateway.quota as quota_mod
    from config import settings

    app = create_app()
    monkeypatch.setattr(settings, "api_key", "")
    # 至少 1 次 failover: 先经历「失败→收录 failed_channels→continue」,
    # 重试再失败才走「注入失败链」的最终分支 (S1 所在路径)
    monkeypatch.setattr(settings, "max_fallback_retries", 1)
    # quota 检查直通 (真实实现会连本地库)
    async def _quota_ok(session, account_id, estimated_tokens=0):
        return True, None
    monkeypatch.setattr(quota_mod, "check_account_quota", _quota_ok)
    async def _user_ok(session, caller_id, estimated_tokens=0):
        return True, None
    monkeypatch.setattr(quota_mod, "check_user_quota", _user_ok)

    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    return client, routes_mod, monkeypatch


class _StubEngine:
    """替代 route_engine: 固定返回一个候选渠道, 不触 DB。"""

    def __init__(self, channel):
        self._channel = channel

    async def _auto_refresh_if_stale(self):
        return None

    def select_channel(self, model, prefs=None, messages=None):
        return self._channel


def _make_stale(status: int, extra_headers: dict) -> JSONResponse:
    """模拟原响应: 带「过期的」content-length 头 (S1 根源 = starlette 对已存在
    的 content-length 不重算), body 实际远大于声明值。"""
    return JSONResponse(
        status_code=status,
        content={"error": {"message": "upstream fail", "type": "upstream_error"}},
        headers={"content-length": "2", **extra_headers},
    )


@pytest.mark.asyncio
async def test_failover_response_content_length_matches_body(env):
    """S1 回归: 最终失败响应的 content-length 必须与实际 body 一致。"""
    client, routes_mod, monkeypatch = env
    channel = {
        "id": 1, "name": "test_channel", "account_id": 997001,
        "account_name": "test_account", "quotas": [],
    }
    monkeypatch.setattr(routes_mod, "route_engine", _StubEngine(channel))
    stale = _make_stale(429, {})

    async def _fake_forward(channel, body, headers, requested_model, actual_model, **kw):
        return stale

    monkeypatch.setattr(routes_mod, "forward_request", _fake_forward)

    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "tier:balanced", "messages": [{"role": "user", "content": "hi"}]},
    )
    await client.aclose()
    assert resp.status_code == 429
    body = resp.content
    declared = int(resp.headers["content-length"])
    assert declared == len(body), (
        f"content-length({declared}) != body({len(body)}): "
        "失败链注入后未重算长度, h11 会断连或客户端截断"
    )
    # 注入的失败链信息必须完整可达客户端
    parsed = json.loads(body)
    assert parsed["error"]["failed_channels"][0]["channel"] == "test_channel"
    assert resp.headers["x-aimc-failed-channels"]


@pytest.mark.asyncio
async def test_failover_response_strips_hop_by_hop_headers(env):
    """hop-by-hop 头 (transfer-encoding/connection) 也不得透传到重包装响应。"""
    client, routes_mod, monkeypatch = env
    channel = {
        "id": 1, "name": "test_channel", "account_id": 997002,
        "account_name": "test_account", "quotas": [],
    }
    monkeypatch.setattr(routes_mod, "route_engine", _StubEngine(channel))
    stale = _make_stale(502, {"connection": "keep-alive", "transfer-encoding": "chunked"})

    async def _fake_forward(channel, body, headers, requested_model, actual_model, **kw):
        return stale

    monkeypatch.setattr(routes_mod, "forward_request", _fake_forward)

    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "tier:balanced", "messages": [{"role": "user", "content": "hi"}]},
    )
    await client.aclose()
    assert resp.status_code == 502
    assert "connection" not in resp.headers
    assert "transfer-encoding" not in resp.headers
    assert int(resp.headers["content-length"]) == len(resp.content)
