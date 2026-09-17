"""V6.7: 流式请求 failover + TPM 限流误分类回归测试.

背景 (2026-09-17 生产双 429 诊断, TraceID 3b485ca5 / 2bff7d95):
- 根因 A: routes.py failover 分支带 `not is_stream` 门 → 流式请求 (ZCode
  全流量) 上游 429/5xx 直接透传客户端, 组内其余候选从不尝试。
- 根因 B: sensenova 对不同账号对同一 TPM 限流返回不同 error.code
  (429001 / insufficient_quota, type 均为 rate_limit_error); modelscope
  免费层 token-limit 429 也是 code=insufficient_quota。分类器按 code 判成
  quota_exhausted (needs_human + 3600s 冷却 + 假告警), 且 insufficient_quota
  透传让客户端判 retryable=false 终止回合。
- 修复 3: 终态 429 语义是限流的 insufficient_quota 改写为 rate_limit_error
  (保留 message), 附 Retry-After: 30。

覆盖:
- classify: sensenova_01 / modelscope 体 → rate_limited (300s, 非 quota)
- classify: 真·计费配额耗尽 (OpenAI 风格, 无限流词) → 仍 quota_exhausted
- routes 流式: 首 fails 429 → failover 第二候选成功 (流式 200)
- routes 流式: 全候选 429 (insufficient_quota+tpm) → 终态改写 + failed_channels
- routes 流式: failover 后流式成功响应不被错误 JSON 包裹

跑法:
    pytest tests/test_v67_stream_failover.py -v
"""

import json

import httpx
import pytest
from fastapi.responses import StreamingResponse


# ============================================================
# 修复 2: classify 限流语义兜底
# ============================================================

# 生产 request_transcripts 10066/10067/10079 原始体
_SENSENOVA_429001 = (
    '{"error":{"message":"inference exceeds tpm/rpm limit",'
    '"type":"rate_limit_error","code":"429001"}}'
)
_SENSENOVA_INSUFFICIENT = (
    '{"error":{"message":"inference exceeds tpm/rpm limit",'
    '"type":"rate_limit_error","code":"insufficient_quota"}}'
)
_MODELSCOPE_TOKEN_LIMIT = (
    '{"error":{"code":"insufficient_quota","message":"You exceeded your current '
    'quota, please check your plan and billing details. For details, see: '
    'https://help.aliyun.com/zh/model-studio/error-code#token-limit",'
    '"param":null,"type":"insufficient_quota"}}'
)
# 真·计费配额耗尽 (无任何限流语义词)
_REAL_QUOTA_EXHAUSTED = (
    '{"error":{"code":"insufficient_quota",'
    '"message":"You exceeded your current quota, please check your plan and '
    'billing details.","type":"insufficient_quota"}}'
)


def test_classify_sensenova_insufficient_quota_is_rate_limited():
    """sensenova_01 体 (code=insufficient_quota + rate_limit_error type + tpm 措辞)
    → rate_limited, 不再误判 quota_exhausted 3600s。"""
    from gateway.error_classifier import ErrorCategory, classify

    info = classify(429, _SENSENOVA_INSUFFICIENT)
    assert info.category == ErrorCategory.RATE_LIMITED
    assert info.needs_human is False
    assert info.suggested_cooldown_seconds == 300


def test_classify_modelscope_token_limit_is_rate_limited():
    """modelscope #token-limit 体 → rate_limited (免费层 TPM, 分钟级自愈)。"""
    from gateway.error_classifier import ErrorCategory, classify

    info = classify(429, _MODELSCOPE_TOKEN_LIMIT)
    assert info.category == ErrorCategory.RATE_LIMITED
    assert info.needs_human is False


def test_classify_429001_still_rate_limited():
    """回归: lixiaoy 体 (429001) 保持 rate_limited 300s。"""
    from gateway.error_classifier import ErrorCategory, classify

    info = classify(429, _SENSENOVA_429001)
    assert info.category == ErrorCategory.RATE_LIMITED
    assert info.suggested_cooldown_seconds == 300


def test_classify_real_quota_exhausted_unchanged():
    """真·计费耗尽 (无限流语义词) 仍判 quota_exhausted + needs_human。"""
    from gateway.error_classifier import ErrorCategory, classify

    info = classify(429, _REAL_QUOTA_EXHAUSTED)
    assert info.category == ErrorCategory.QUOTA_EXHAUSTED
    assert info.needs_human is True


def test_classify_non_429_with_rate_limit_word_not_hinted():
    """hint 分支只对 429 生效 — 非 429 不因文案含限流词被判 rate_limited。"""
    from gateway.error_classifier import ErrorCategory, classify

    info = classify(400, _SENSENOVA_INSUFFICIENT)
    assert info.category != ErrorCategory.RATE_LIMITED


# ============================================================
# 修复 1 + 3: 流式 failover / 终态改写 (真实 ASGI, mock 边界)
# ============================================================

def _channel(name="ch_a", account_id=998001):
    return {
        "id": 1, "name": name, "account_id": account_id,
        "account_name": f"{name}_acct", "quotas": [],
    }


class _StubEngine:
    def __init__(self, channel):
        self._channel = channel

    async def _auto_refresh_if_stale(self):
        return None

    def select_channel(self, model, prefs=None, messages=None):
        return self._channel

    def sibling_account_ids(self, channel_id, account_id):
        return []


@pytest.fixture
def env(monkeypatch):
    from gateway.app import create_app
    import gateway.routes as routes_mod
    import gateway.quota as quota_mod
    from config import settings

    app = create_app()
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "max_fallback_retries", 1)

    async def _quota_ok(session, account_id, estimated_tokens=0):
        return True, None

    async def _user_ok(session, caller_id, estimated_tokens=0):
        return True, None

    monkeypatch.setattr(quota_mod, "check_account_quota", _quota_ok)
    monkeypatch.setattr(quota_mod, "check_user_quota", _user_ok)

    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    return client, routes_mod, monkeypatch


def _err_response(status, payload):
    from starlette.responses import Response

    return Response(
        content=json.dumps(payload),
        status_code=status,
        media_type="application/json",
    )


@pytest.mark.asyncio
async def test_stream_429_fails_over_to_next_attempt(env):
    """修复 1 核心: 流式请求上游 429 后 failover 第二次尝试成功。"""
    client, routes_mod, monkeypatch = env
    monkeypatch.setattr(routes_mod, "route_engine", _StubEngine(_channel()))
    calls = {"n": 0}

    async def _fake_forward(channel, body, headers, requested_model, actual_model, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return _err_response(429, _json_err(_SENSENOVA_INSUFFICIENT))
        return StreamingResponse(
            iter([b'data: {"ok":true}\n\ndata: [DONE]\n\n']),
            status_code=200,
            media_type="text/event-stream",
        )

    monkeypatch.setattr(routes_mod, "forward_request", _fake_forward)

    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "tier:strong", "stream": True,
              "messages": [{"role": "user", "content": "hi"}]},
    )
    await client.aclose()
    assert calls["n"] == 2, "429 后必须重试第二候选"
    assert resp.status_code == 200
    assert b'"ok":true' in resp.content


@pytest.mark.asyncio
async def test_stream_terminal_429_rewrites_insufficient_quota(env):
    """修复 3: 全候选 429 (insufficient_quota + tpm 措辞) → 终态改写
    rate_limit_error + Retry-After + failed_channels 注入。"""
    client, routes_mod, monkeypatch = env
    monkeypatch.setattr(routes_mod, "route_engine", _StubEngine(_channel()))

    async def _fake_forward(channel, body, headers, requested_model, actual_model, **kw):
        return _err_response(429, _json_err(_SENSENOVA_INSUFFICIENT))

    monkeypatch.setattr(routes_mod, "forward_request", _fake_forward)

    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "tier:strong", "stream": True,
              "messages": [{"role": "user", "content": "hi"}]},
    )
    await client.aclose()
    assert resp.status_code == 429
    parsed = json.loads(resp.content)
    err = parsed["error"]
    assert err["code"] == "rate_limit_exceeded", f"终态必须改写, got {err.get('code')}"
    assert err["type"] == "rate_limit_error"
    # 原始 message 保留供诊断
    assert "tpm/rpm" in err["message"]
    assert err["failed_channels"][0]["channel"] == "ch_a"
    assert resp.headers.get("retry-after") == "30"
    assert resp.headers.get("x-aimc-last-error-type") == "rate_limited"


@pytest.mark.asyncio
async def test_stream_terminal_429_real_quota_not_rewritten(env):
    """真·计费耗尽 (无限流词) 终态不改写 — code 保持 insufficient_quota。"""
    client, routes_mod, monkeypatch = env
    monkeypatch.setattr(routes_mod, "route_engine", _StubEngine(_channel()))

    async def _fake_forward(channel, body, headers, requested_model, actual_model, **kw):
        return _err_response(429, _json_err(_REAL_QUOTA_EXHAUSTED))

    monkeypatch.setattr(routes_mod, "forward_request", _fake_forward)

    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "tier:strong", "stream": True,
              "messages": [{"role": "user", "content": "hi"}]},
    )
    await client.aclose()
    assert resp.status_code == 429
    parsed = json.loads(resp.content)
    assert parsed["error"]["code"] == "insufficient_quota"


@pytest.mark.asyncio
async def test_stream_success_after_failover_not_wrapped(env):
    """failover 后流式成功响应不得被错误 JSON 包裹 (body_iterator 守卫)。"""
    client, routes_mod, monkeypatch = env
    monkeypatch.setattr(routes_mod, "route_engine", _StubEngine(_channel()))
    calls = {"n": 0}

    async def _fake_forward(channel, body, headers, requested_model, actual_model, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return _err_response(502, {"error": {"message": "bad gateway"}})
        return StreamingResponse(
            iter([b"chunk-1", b"chunk-2"]),
            status_code=200,
            media_type="text/event-stream",
        )

    monkeypatch.setattr(routes_mod, "forward_request", _fake_forward)

    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "tier:strong", "stream": True,
              "messages": [{"role": "user", "content": "hi"}]},
    )
    await client.aclose()
    assert resp.status_code == 200
    assert resp.content == b"chunk-1chunk-2"
    assert b"Upstream error" not in resp.content


def _json_err(body_text):
    return json.loads(body_text)
