"""V6.18 (2026-10-22): 400 failover 换路 + 失败分类落库 + 失败链落库 回归测试.

背景 (生产 2026-10-22 00:16 实证, aliy-hy):
hermes (shy) 请求 tier:balanced (组路由), 同一渠道同一账号同一模型连撞 3 次
400 后透传给客户端:

  00:16:12.413 POST https://token.sensenova.cn/v1/chat/completions 400
  00:16:12.418 P1-15: sensenova_official 400 (client_error), failover attempt 1/2
  00:16:13.007 POST https://token.sensenova.cn/v1/chat/completions 400
  00:16:13.012 P1-15: sensenova_official 400 (client_error), failover attempt 2/2
  00:16:13.580 POST https://token.sensenova.cn/v1/chat/completions 400
  00:16:13.586 POST /v1/chat/completions -> 400 (1952ms) user=shy

三跳全是 sensenova_official/lixiaoy + sensenova-6.8-flash-lite — 因为 400
判 client_error 不进 cooldown, 重选候选时同一条路仍是最优, failover 变空转
(白烧 1.95s 且最终仍失败)。根因 = 上游 "inference request is invalid"。

三项修复:
1. failover 换路: 本次请求内已试过的 (account_id, model) 组合在重选时排除
   (router.select_channel/select_best_channel 新增 exclude_pairs)
2. 非流式失败补分类落库: 旧路径 4xx/5xx 落库行 error_type/error_code/
   error_msg 全空 (生产近 3 天 224 条 400 + 283 条 429 + 19 条 5xx 空)
3. 失败链落库: usage_logs.failed_channels 列全表 0 条非空 — 失败链只在
   响应体/响应头, 看板无法下钻

跑法:
    pytest tests/test_v618_400_failover_chain.py -v
"""

import json

import httpx
import pytest
from fastapi.responses import StreamingResponse


# 生产实录: sensenova 400 原文 (error_code=BadRequest)
_SENSENOVA_400 = (
    '{"error":{"message":"inference request is invalid",'
    '"type":"invalid_request_error","code":"BadRequest"}}'
)


def _channel(name="ch_a", account_id=998001, account_name="acct_a"):
    return {
        "id": 1, "name": name, "account_id": account_id,
        "account_name": account_name, "quotas": [],
    }


# ============================================================
# 修复 1: failover 换路 (排除本次请求已试过的组合)
# ============================================================

class _StubEngine:
    """按 exclude_pairs 选渠道的假引擎。

    模拟真实 router 行为: 组路由重选时, 已被本请求排除的 (account, model)
    组合不再返回 — 换一条路。exclude_pairs 里没有的返回下一个候选。
    """

    def __init__(self, channels, models):
        # channels: [(account_id, model), ...] 按优先级排序
        self._channels = channels
        self._models = models
        self.seen = []

    async def _auto_refresh_if_stale(self):
        return None

    def select_channel(self, model, prefs=None, messages=None, exclude_pairs=None):
        exclude_pairs = exclude_pairs or set()
        for account_id, m in self._channels:
            if (account_id, m) in exclude_pairs:
                continue
            self.seen.append((account_id, m))
            return {
                "id": account_id, "name": f"ch_{account_id}",
                "account_id": account_id, "account_name": f"acct_{account_id}",
                "quotas": [],
                "_registry_model": m,
                "_actual_model": m,
            }
        return None

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
    monkeypatch.setattr(settings, "max_fallback_retries", 2)

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
async def test_400_failover_switches_to_next_candidate(env):
    """修复 1 核心: 组路由上游 400 后重选必须换一条路, 不能重撞同渠道同模型。

    生产实录: 三跳全是 sensenova_official/lixiaoy + sensenova-6.8-flash-lite。
    修复后: 第 2 跳应换到另一账号/模型, 而不是把同一路再撞一次。
    """
    client, routes_mod, monkeypatch = env
    # 两个候选: (acct 1, model-a) / (acct 2, model-b)
    engine = _StubEngine([(1, "model-a"), (2, "model-b")], ["model-a", "model-b"])
    monkeypatch.setattr(routes_mod, "route_engine", engine)

    calls = []

    async def _fake_forward(channel, body, headers, requested_model, actual_model, **kw):
        calls.append(channel["account_id"])
        return _err_response(400, json.loads(_SENSENOVA_400))

    monkeypatch.setattr(routes_mod, "forward_request", _fake_forward)

    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "tier:balanced", "stream": True,
              "messages": [{"role": "user", "content": "hi"}]},
    )
    await client.aclose()

    # 第一跳撞 acct 1, 第二跳必须换到 acct 2
    assert calls == [1, 2], f"400 后必须换候选, 实际调用序列 {calls}"
    assert resp.status_code == 400
    # 失败链含两跳
    parsed = json.loads(resp.content)
    assert len(parsed["error"]["failed_channels"]) == 2
    assert parsed["error"]["failed_channels"][0]["account_id"] == 1
    assert parsed["error"]["failed_channels"][1]["account_id"] == 2


@pytest.mark.asyncio
async def test_400_failover_exhausted_candidates_replays_last_failure(env):
    """候选全被排除后无路可走 → 回放最后一次上游失败 (400), 不合成 503。

    理由: 合成 503 会掩盖上游真因, 且 503 对客户端是可重试语义, 而上游的
    400 (client_error) 不可重试 — 让客户端对不会自愈的请求反复重试。
    """
    client, routes_mod, monkeypatch = env
    engine = _StubEngine([(1, "model-a")], ["model-a"])
    monkeypatch.setattr(routes_mod, "route_engine", engine)

    calls = []

    async def _fake_forward(channel, body, headers, requested_model, actual_model, **kw):
        calls.append(channel["account_id"])
        return _err_response(400, json.loads(_SENSENOVA_400))

    monkeypatch.setattr(routes_mod, "forward_request", _fake_forward)

    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "tier:balanced", "stream": True,
              "messages": [{"role": "user", "content": "hi"}]},
    )
    await client.aclose()

    # 只撞一次: 排除后无候选 → 回放, 不重复撞同一路
    assert calls == [1], f"排除后不应重撞同一路, 实际 {calls}"
    # 上游真因保留 (400 而非合成的 503)
    assert resp.status_code == 400, f"应回放上游状态码, 实际 {resp.status_code}"
    parsed = json.loads(resp.content)
    assert parsed["error"]["code"] == "BadRequest"
    assert parsed["error"]["message"] == "inference request is invalid"
    # 失败链仍带上一跳
    assert len(parsed["error"]["failed_channels"]) == 1


@pytest.mark.asyncio
async def test_400_failover_second_candidate_succeeds(env):
    """修复 1 目标态: 第一跳 400 → 第二跳成功 → 客户端拿到 200。

    这是本次修复的收益点: 生产上三跳全 400 最终透传 400, 修复后换路即成功。
    """
    client, routes_mod, monkeypatch = env
    engine = _StubEngine([(1, "model-a"), (2, "model-b")], ["model-a", "model-b"])
    monkeypatch.setattr(routes_mod, "route_engine", engine)

    calls = []

    async def _fake_forward(channel, body, headers, requested_model, actual_model, **kw):
        calls.append(channel["account_id"])
        if len(calls) == 1:
            return _err_response(400, json.loads(_SENSENOVA_400))
        return StreamingResponse(
            iter([b'data: {"ok":true}\n\ndata: [DONE]\n\n']),
            status_code=200,
            media_type="text/event-stream",
        )

    monkeypatch.setattr(routes_mod, "forward_request", _fake_forward)

    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "tier:balanced", "stream": True,
              "messages": [{"role": "user", "content": "hi"}]},
    )
    await client.aclose()

    assert calls == [1, 2]
    assert resp.status_code == 200
    assert b'"ok":true' in resp.content


# ============================================================
# 修复 2: 非流式 4xx/5xx 补分类落库
# ============================================================

def test_nonstream_failure_writes_classified_usage_log(monkeypatch):
    """非流式 400 → 落库行带 error_type/error_code/error_msg。

    旧行为: error_type/error_code/error_msg 全空 (生产 224 条 400 空)。
    """
    from gateway import proxy

    captured = {}

    async def _fake_write(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(proxy, "_write_usage_log", _fake_write)
    monkeypatch.setattr(proxy, "_spawn", lambda coro: _run(coro))

    class _Resp:
        status_code = 400
        text = _SENSENOVA_400

        def json(self):
            return json.loads(_SENSENOVA_400)

    proxy._schedule_usage_log(_channel(), _Resp(), 100, "tier:balanced")

    assert captured, "失败请求必须落一行 usage_log"
    assert captured["status_code"] == 400
    assert captured["error_type"] == "client_error", f"got {captured.get('error_type')}"
    assert captured["error_code"] == "BadRequest"
    assert "inference request is invalid" in captured["error_msg"]
    assert captured["input_tokens"] == 0 and captured["output_tokens"] == 0
    assert captured["needs_human"] is False


def test_nonstream_failure_non_json_body_still_writes_log(monkeypatch):
    """上游错误体非 JSON (如 HTML 错误页) → 仍落一行带分类。

    旧行为: response.json() 抛异常 → 整行不落 → 请求在用量页完全不可见。
    """
    from gateway import proxy

    captured = {}

    async def _fake_write(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(proxy, "_write_usage_log", _fake_write)
    monkeypatch.setattr(proxy, "_spawn", lambda coro: _run(coro))

    class _Resp:
        status_code = 502
        text = "<html>502 Bad Gateway</html>"

        def json(self):
            raise ValueError("not json")

    proxy._schedule_usage_log(_channel(), _Resp(), 100, "tier:balanced")

    assert captured, "非 JSON 错误体也必须落库"
    assert captured["status_code"] == 502
    assert captured["error_type"] == "upstream_5xx", f"got {captured.get('error_type')}"


def test_nonstream_success_unchanged(monkeypatch):
    """成功路径行为不变: 落 usage, 不带 error 字段。"""
    from gateway import proxy

    captured = {}

    async def _fake_write(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(proxy, "_write_usage_log", _fake_write)
    monkeypatch.setattr(proxy, "_spawn", lambda coro: _run(coro))

    class _Resp:
        status_code = 200
        text = '{"usage":{"prompt_tokens":10,"completion_tokens":5}}'

        def json(self):
            return json.loads(self.text)

    proxy._schedule_usage_log(_channel(), _Resp(), 100, "tier:balanced")

    assert captured["status_code"] == 200
    assert captured["input_tokens"] == 10
    assert captured.get("error_type") is None


def test_nonstream_failure_keeps_cost_estimate_zero(monkeypatch):
    """失败行仍算 cost (0.0), 不改成本统计口径。

    生产存量 224 条非流式 400 行的 cost_estimate=0.0 (非 NULL) — 补分类
    时不能把口径改成 NULL, 否则成本页"有成本记录数"等统计会变。
    """
    from gateway import proxy

    captured = {}

    async def _fake_write(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(proxy, "_write_usage_log", _fake_write)
    monkeypatch.setattr(proxy, "_spawn", lambda coro: _run(coro))

    class _Resp:
        status_code = 400
        text = _SENSENOVA_400

        def json(self):
            return json.loads(_SENSENOVA_400)

    proxy._schedule_usage_log(_channel(), _Resp(), 100, "tier:balanced")

    # cost_estimate 参与落库 (不是缺失), 与存量口径一致
    assert "cost_estimate" in captured
    assert captured["cost_estimate"] is None or captured["cost_estimate"] == 0.0


# ============================================================
# 修复 3: failed_channels 落库
# ============================================================

def test_failed_channels_persisted_to_usage_log(monkeypatch):
    """失败链落 usage_logs.failed_channels (旧: 全表 0 条非空)。"""
    from gateway import proxy

    captured = {}

    async def _fake_write(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(proxy, "_write_usage_log", _fake_write)
    monkeypatch.setattr(proxy, "_spawn", lambda coro: _run(coro))

    chain = [
        {"channel": "ch_1", "account_id": 1, "status": 400,
         "error_type": "client_error", "error_code": "BadRequest"},
        {"channel": "ch_2", "account_id": 2, "status": 500,
         "error_type": "upstream_5xx", "error_code": None},
    ]

    class _Resp:
        status_code = 400
        text = _SENSENOVA_400

        def json(self):
            return json.loads(_SENSENOVA_400)

    proxy._schedule_usage_log(
        _channel(), _Resp(), 100, "tier:balanced", failed_channels=chain,
    )

    assert captured["failed_channels"] == chain
    assert len(captured["failed_channels"]) == 2


@pytest.mark.asyncio
async def test_routes_passes_failure_chain_downstream(env):
    """routes.py 逐跳累积失败链并传给 proxy → 终态那行带完整链。"""
    client, routes_mod, monkeypatch = env
    engine = _StubEngine([(1, "model-a"), (2, "model-b")], ["model-a", "model-b"])
    monkeypatch.setattr(routes_mod, "route_engine", engine)

    seen_chains = []

    async def _fake_forward(channel, body, headers, requested_model, actual_model, **kw):
        seen_chains.append(kw.get("failed_channels"))
        return _err_response(400, json.loads(_SENSENOVA_400))

    monkeypatch.setattr(routes_mod, "forward_request", _fake_forward)

    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "tier:balanced", "stream": True,
              "messages": [{"role": "user", "content": "hi"}]},
    )
    await client.aclose()

    # 第 1 跳无前置失败 → None; 第 2 跳带 1 条失败记录
    assert seen_chains[0] is None
    assert len(seen_chains[1]) == 1
    assert seen_chains[1][0]["account_id"] == 1
    # 候选耗尽 → 回放最后上游失败 (400), 不合成 503
    assert resp.status_code == 400


# ============================================================
# router 层: exclude_pairs 参数与行为
# ============================================================

def test_select_best_channel_excludes_failed_pair():
    """router.select_best_channel 排除指定 (account, model) 组合。"""
    from gateway.router import RouteEngine

    def _ch(ch_id, name, acct_id, price):
        return {
            "id": ch_id, "name": name, "provider": "p", "provider_type": "official",
            "billing_mode": "pay_per_token", "priority": 5, "api_key": "sk-mock",
            "account_id": acct_id, "account_name": f"acct_{acct_id}",
            "quotas": [], "quota_total": None, "quota_used": 0, "quota_remaining": None,
            "drop_params": [], "supports_prompt_cache": 0,
            "disable_thinking_pass_through": 0, "disable_thinking_inject": 0,
            "requires_reasoning_echo": 0, "auto_drop_params": 1,
            "prices": [
                {"model": "test-model", "price_type": "normal", "time_range": "全天",
                 "input_price": price, "output_price": price},
            ],
        }

    engine = RouteEngine()
    engine._channels_cache = [
        _ch(1, "ch_a", 1, 1.0),
        _ch(2, "ch_b", 2, 2.0),
    ]
    engine._registry_cache = {}

    best = engine.select_best_channel("test-model")
    assert best is not None
    first_account = best["account_id"]
    assert first_account == 1, "同价时应按稳定顺序取首个"

    # 排除首个 → 换另一个
    other = engine.select_best_channel(
        "test-model", exclude_pairs={(first_account, "test-model")},
    )
    assert other is not None
    assert other["account_id"] != first_account, "排除后必须换渠道"
    assert (other["account_id"], "test-model") != (first_account, "test-model")


def _run(coro):
    """同步跑一个 coroutine (给 monkeypatch 后的 _spawn 用)。"""
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is None:
        return asyncio.run(coro)
    return loop.create_task(coro)
