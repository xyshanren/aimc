"""V6.x 超时连击软降级测试 (2026-11-04)

机制: 第三类同价层换手信号 — 60min 内上游超时 (504) ≥ 阈值 → 同价层软降级
N 分钟 (内存计数; 超时不写 cooldown_events, 429 突发的 DB 计数路子数不到它)。
与 429 突发共用 _burst_demote_until 降级位; V6.10 大请求收敛尊重降级态
(降级渠道不收敛, 保持原候选交给同价层排序)。

背景: 11-03 晚 taotoken 上游对 ~370k+ input 大请求全 120s 超时, 每笔白等
120s 才 failover (~28 笔), 且超时无任何 DB 痕 — 看板只见"20:33 后消失"。

跑法: python -m pytest tests/test_router_timeout_burst_demote.py -v
"""
import logging
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from gateway.router import RouteEngine


@pytest.fixture
def engine(monkeypatch):
    """超时突发参数打桩。

    注意钉的是 gateway.router 模块内绑定的 settings 引用 (record_upstream_timeout
    实际读的对象), 而非 from config import settings —— 老测试文件 (cleanup_/
    null_price_/phase4_e1_) 会在模块顶层把 config.settings 换成 _MockSettings,
    子集跑时该 mock 会泄漏进 router (全量按字母序本文件在前不触发)。"""
    import gateway.router as router_mod
    s = router_mod.settings
    monkeypatch.setattr(s, "timeout_burst_threshold", 3, raising=False)
    monkeypatch.setattr(s, "timeout_burst_window_minutes", 60, raising=False)
    monkeypatch.setattr(s, "timeout_burst_minutes", 10, raising=False)
    return RouteEngine()


def _item(name, priority, price=0.0, billing="subscription"):
    """组路由候选条目 (channel, model_id, price, family_bonus, member_priority, channel_model)
    注意: subscription/free 计费在 sort key 里强制 0 元 (边际成本语义),
    非 0 价格断言必须用 pay_per_token。"""
    return ({"name": name, "priority": priority, "billing_mode": billing},
            "glm-5.3-flash", price, 0, 100, name)


class TestTimeoutBurstDemote:
    def test_below_threshold_no_demote(self, engine):
        """2 次 (< 阈值 3) → 不降级"""
        for _ in range(2):
            engine.record_upstream_timeout("tt")
        assert engine._is_demoted("tt") is False
        assert "tt" not in engine._burst_demote_until

    def test_burst_triggers_demote_and_reorder(self, engine):
        """3 次连击 → 降级; 同价层内排后, price 层不可动摇"""
        for _ in range(3):
            engine.record_upstream_timeout("tt")
        assert engine._is_demoted("tt") is True
        # 同价层内: 降级的 tt 排到健康 gb 之后
        assert engine._dynamic_sort_key(_item("gb", 8)) < engine._dynamic_sort_key(_item("tt", 9))
        # price 第一关键字: 降级的 0 元渠道仍排在 0.5 元付费之前 (付费须 pay_per_token,
        # subscription 会被 sort key 强制归 0)
        paid = _item("ch1", 10, price=0.5, billing="pay_per_token")
        assert engine._dynamic_sort_key(_item("tt", 9)) < engine._dynamic_sort_key(paid)

    def test_demote_logs_once_per_cycle(self, engine, caplog):
        """首次触发记 1 条 [超时突发降级], 降级中再超时只延期不刷屏"""
        caplog.set_level(logging.WARNING, logger="gateway.router")
        for _ in range(5):
            engine.record_upstream_timeout("tt")
        warns = [r for r in caplog.records
                 if r.levelno >= logging.WARNING and "超时突发降级" in r.getMessage()]
        assert len(warns) == 1

    def test_window_prune(self, engine):
        """60min 窗外旧事件不计数 (滑窗裁剪)"""
        now_ts = time.time()
        engine._timeout_events["tt"] = [now_ts - 3601, now_ts - 3599]
        engine.record_upstream_timeout("tt")  # 裁掉 3601 旧事件, 窗内 2 → 不足 3
        assert engine._is_demoted("tt") is False
        engine.record_upstream_timeout("tt")  # 窗内 3 → 触发
        assert engine._is_demoted("tt") is True

    def test_demote_expiry(self, engine):
        """降级到期自动失效 (sort 侧 now < until 判定)"""
        for _ in range(3):
            engine.record_upstream_timeout("tt")
        assert engine._is_demoted("tt") is True
        engine._burst_demote_until["tt"] = time.time() - 1
        assert engine._is_demoted("tt") is False

    def test_timeout_demote_survives_429_refresh_cleanup(self, engine):
        """回归护栏: 旧的"滑出 429 白名单即清"规则会把不在白名单里的超时
        降级在每轮 60s 刷新时误清 — 现为到期清理, 未过期条目保留"""
        for _ in range(3):
            engine.record_upstream_timeout("tt")
        assert engine._is_demoted("tt") is True
        engine._update_burst_state({})  # 60s 刷新轮空转 (无 429 事件)
        assert engine._is_demoted("tt") is True

    def test_429_and_timeout_coexist(self, engine):
        """429 突发与超时突发共用降级位, 互不清对方"""
        engine._update_burst_state({"glm_bigmodel": 5})  # 429 突发降级 gb
        assert engine._is_demoted("glm_bigmodel") is True
        for _ in range(3):
            engine.record_upstream_timeout("tt")
        assert engine._is_demoted("tt") is True
        k_tt = engine._dynamic_sort_key(_item("tt", 9))
        k_gb = engine._dynamic_sort_key(_item("glm_bigmodel", 8))
        assert k_tt[1] == 1 and k_gb[1] == 1  # 两个都进降级位

    def test_convergence_skips_demoted_taotoken(self, engine, monkeypatch, caplog):
        """V6.10 收敛让路: taotoken 降级时不再收敛 (保持原候选), 健康时照常收敛"""
        import gateway.router as router_mod
        settings = router_mod.settings  # 与 router 同源, 防 _MockSettings 泄漏错位

        def _ch(name, price_val, priority):
            return {
                "id": hash(name) % 10000, "name": name,
                "account_id": 9000 + (hash(name) % 100),
                "account_name": name + "_01", "priority": priority,
                "status": "active", "quotas": [],
                "quota_remaining": None, "quota_total": None,
                "billing_mode": "subscription", "provider_type": "official",
                "prices": [{"model": "glm-5.3-flash", "price_type": "normal",
                            "time_range": "全天", "input_price": price_val,
                            "output_price": price_val, "is_promotion": 0}],
            }

        tt = _ch("taotoken_coding", 0.0, 9)
        gb = _ch("glm_bigmodel", 0.0, 8)
        paid = _ch("ch1_paid", 0.5, 10)
        engine._channels_cache = [tt, gb, paid]
        monkeypatch.setattr(settings, "channel_min_input_tokens",
                            {"taotoken_coding": 7000}, raising=False)
        monkeypatch.setattr(settings, "channel_max_input_tokens", {}, raising=False)
        big_messages = [{"role": "user", "content": "tok " * 40000}]  # est ~10k ≥ 7000

        caplog.set_level(logging.INFO, logger="gateway.router")

        # 健康态: 收敛触发 (日志有收敛行), 返回 taotoken
        caplog.clear()
        picked = engine.select_best_channel("glm-5.3-flash", messages=big_messages)
        conv_logs = [r for r in caplog.records if "候选收敛到 taotoken_coding" in r.getMessage()]
        assert len(conv_logs) >= 1
        assert picked is not None and picked["name"] == "taotoken_coding"

        # 降级态: 收敛让路 (无收敛行), 候选保持 → 由后续排序决定
        caplog.clear()
        for _ in range(3):
            engine.record_upstream_timeout("taotoken_coding")
        picked2 = engine.select_best_channel("glm-5.3-flash", messages=big_messages)
        conv_logs2 = [r for r in caplog.records if "候选收敛到 taotoken_coding" in r.getMessage()]
        assert len(conv_logs2) == 0
        assert picked2 is not None  # 宁试不 503: 仍有候选可选


class TestProxyTimeoutLogging:
    """V6.x 超时失败落库 (2026-11-04): 504 不再看板隐身"""

    def test_timeout_writes_usage_log_and_strikes(self, monkeypatch):
        """上游超时 → 504 + usage_logs 一行 (status=504, upstream_5xx) + 超时计数 +1"""
        import asyncio
        import json
        import httpx
        import gateway.proxy as proxy_mod
        from gateway.router import route_engine

        def handler(request):
            raise httpx.ConnectTimeout("timed out", request=request)

        transport = httpx.MockTransport(handler)
        orig_client = httpx.AsyncClient
        httpx.AsyncClient = lambda *a, **kw: orig_client(transport=transport, *a, **kw)

        captured = []

        async def fake_write(**kw):
            captured.append(kw)

        monkeypatch.setattr(proxy_mod, "_write_usage_log", fake_write)

        saved_events = route_engine._timeout_events
        saved_until = dict(route_engine._burst_demote_until)
        route_engine._timeout_events = {}
        route_engine._burst_demote_until = {}
        try:
            channel = {
                "id": 321, "account_id": 654, "name": "mockchan",
                "account_name": "mockchan_01", "api_key": "test-key",
                "base_url": "https://mock.test/v1", "provider_type": "official",
            }
            body = json.dumps({
                "model": "m1",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
            }).encode()

            resp = asyncio.run(proxy_mod.forward_request(
                channel, body, {},
                requested_model="m1", actual_model="m1",
                caller_id=None, caller_key_id=None,
                failed_channels=None,
            ))

            assert resp.status_code == 504
            assert len(captured) == 1
            row = captured[0]
            assert row["status_code"] == 504
            assert row["error_type"] == "upstream_5xx"
            assert row["channel_id"] == 321
            assert row["account_id"] == 654
            assert row["model"] == "m1"
            assert row["input_tokens"] == 0 and row["output_tokens"] == 0
            # 超时计数进内存信号源 (1 次 < 阈值 3, 不降级)
            assert len(route_engine._timeout_events.get("mockchan", [])) == 1
            assert "mockchan" not in route_engine._burst_demote_until
        finally:
            httpx.AsyncClient = orig_client
            route_engine._timeout_events = saved_events
            route_engine._burst_demote_until = saved_until
        # 断言之外的卫生断言: 恢复后计数状态回到测试前
        assert route_engine._timeout_events is saved_events
