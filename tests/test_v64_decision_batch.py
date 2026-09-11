"""V6.4 决策批次 (2026-09-11) 测试:

D1 思考 Phase B — gateway.proxy._inject_thinking_off (渠道开 disable_thinking_inject=1
                  时注入 enable_thinking=false, 客户端显式传参不覆盖)
D2 F3 S3      — intelligence.model_sync confidence 门槛 (AUTO_JOIN_MIN_CONFIDENCE=0.9,
                  仅约束非 T0 自动入组; AUTO_JOIN_NON_T0 已开启)
D4 #11/#12    — gateway.router cache-aware 有效输入价 (hit=0 退化原价 /
                  hit>0 折算排序 / 无 cache 价不受影响)
"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(ROOT))

import pytest

from gateway.proxy import _inject_thinking_off
from intelligence.model_sync import (
    AUTO_JOIN_MIN_CONFIDENCE,
    AUTO_JOIN_NON_T0,
    should_auto_join,
)
from schemas.intel import ModelIntelItem


# ============================================================
# D1: Phase B 注入
# ============================================================

class TestInjectThinkingOff:
    def test_injects_when_absent(self):
        body = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
        out = _inject_thinking_off(body)
        assert out["enable_thinking"] is False
        assert "model" in out and "messages" in out

    def test_respects_client_explicit_true(self):
        body = {"model": "m", "enable_thinking": True}
        out = _inject_thinking_off(body)
        assert out is body  # 不覆盖, 返回原 dict

    def test_respects_client_explicit_false(self):
        body = {"model": "m", "enable_thinking": False}
        out = _inject_thinking_off(body)
        assert out is body

    def test_does_not_modify_original(self):
        body = {"model": "m", "messages": []}
        out = _inject_thinking_off(body)
        assert "enable_thinking" not in body  # 原 dict 不被污染
        assert out is not body

    def test_empty_body(self):
        out = _inject_thinking_off({})
        assert out == {"enable_thinking": False}


class TestAutoJoinSwitches:
    """V6.4 S3: AUTO_JOIN_NON_T0=True + 门槛 0.9 生效 (决策快照测试)"""

    def test_switches_on(self):
        assert AUTO_JOIN_NON_T0 is True
        assert AUTO_JOIN_MIN_CONFIDENCE == 0.9


# ============================================================
# D2: S3 confidence 门槛
# ============================================================

def _item(conf: float, tier="T1") -> ModelIntelItem:
    return ModelIntelItem(
        model_id="test-model",
        confidence=conf,
        inferred_tier=tier,
        family="test",
        source_provider="test",
    )


class TestS3ConfidenceGate:
    def test_t1_high_conf_joins(self):
        assert should_auto_join(_item(0.95)) is True

    def test_t1_below_gate_no_join(self):
        # 0.85: 过 JOIN_THRESHOLD 0.8 但不过 S3 门槛 0.9 → 不可入组
        assert should_auto_join(_item(0.85)) is False

    def test_t1_at_gate_joins(self):
        assert should_auto_join(_item(0.9)) is True

    def test_t0_exempt_from_gate(self):
        # T0 恒免人工: conf 0.8 (>= JOIN_THRESHOLD) 即可, 不受 0.9 门槛约束
        assert should_auto_join(_item(0.8, tier="T0")) is True

    def test_no_tier_no_join(self):
        assert should_auto_join(_item(0.95, tier=None)) is False

    def test_low_conf_no_join(self):
        assert should_auto_join(_item(0.5)) is False


# ============================================================
# D4: cache-aware 有效输入价
# ============================================================

from gateway.router import RouteEngine


def _engine_with_hit(hit: float) -> RouteEngine:
    eng = RouteEngine()
    eng._cache_hit_cache["ch-hit"] = (float("inf"), hit)  # inf future → 永不过期
    eng._cache_hit_cache["ch-zero"] = (float("inf"), 0.0)
    return eng


def _channel(name: str, prices: list) -> dict:
    return {"name": name, "prices": prices}


class TestCacheAwareInputPrice:
    def _price(self, input_price=10.0, cached=None):
        p = {"model": "m", "time_range": "", "input_price": input_price,
             "output_price": 20.0, "is_promotion": 0}
        if cached is not None:
            p["cached_input_price"] = cached
        return p

    def test_no_cache_price_original(self):
        eng = _engine_with_hit(0.9)
        ch = _channel("ch-hit", [self._price(input_price=10.0)])  # 无 cached_input_price
        assert eng._get_input_price(ch, "m") == 10.0

    def test_hit_zero_degrades_to_original(self):
        eng = _engine_with_hit(0.0)
        ch = _channel("ch-zero", [self._price(input_price=10.0, cached=1.0)])
        assert eng._get_input_price(ch, "m") == 10.0

    def test_hit_discounts_price(self):
        eng = _engine_with_hit(0.9)
        ch = _channel("ch-hit", [self._price(input_price=10.0, cached=1.0)])
        # 0.9*1.0 + 0.1*10.0 = 1.9
        assert eng._get_input_price(ch, "m") == pytest.approx(1.9)

    def test_cached_ge_input_no_discount(self):
        eng = _engine_with_hit(0.9)
        ch = _channel("ch-hit", [self._price(input_price=10.0, cached=12.0)])
        assert eng._get_input_price(ch, "m") == 10.0

    def test_no_hit_data_channel_degrades(self):
        eng = _engine_with_hit(0.9)
        ch = _channel("ch-unknown", [self._price(input_price=10.0, cached=1.0)])
        assert eng._get_input_price(ch, "m") == 10.0

    def test_null_price_999_fallback(self):
        eng = _engine_with_hit(0.9)
        ch = _channel("ch-hit", [{"model": "m", "time_range": "", "input_price": None,
                                  "output_price": 20.0, "is_promotion": 0}])
        assert eng._get_input_price(ch, "m") == 999.0

    def test_sort_reversal_integration(self):
        """deepseek 高命中低价 vs sensenova 零命中: 折算后排序应反转"""
        from gateway.router import _group_cheapest_sort_key
        eng = _engine_with_hit(0.9)
        # A: 原价 5 元, 90% 命中, cache 价 0.5 → eff = 0.9*0.5+0.1*5 = 0.95
        eff_a = eng._get_input_price(
            _channel("ch-hit", [self._price(input_price=5.0, cached=0.5)]), "m")
        # B: 原价 4 元, 零命中 → eff = 4.0 (原价更低但不折算)
        eff_b = eng._get_input_price(
            _channel("ch-zero", [self._price(input_price=4.0, cached=0.4)]), "m")
        assert eff_a < eff_b  # 折算后 A 反超
        key_a = _group_cheapest_sort_key(({"billing_mode": "pay_per_token"}, "m", eff_a, 0, 100, "cm"))
        key_b = _group_cheapest_sort_key(({"billing_mode": "pay_per_token"}, "m", eff_b, 0, 100, "cm"))
        assert key_a < key_b
