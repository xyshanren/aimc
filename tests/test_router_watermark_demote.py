"""V6.x 水位线/429 突发软降级测试 (2026-11-01)

机制: 同价层主动换手 — (a) 按次计费渠道 5h 滚动调用数对照帽值 (滞回:
≥60% 排序降级 / <40% 回升); (b) 60min 内 ≥3 次 rate_limited → 软降级 10min。
price 第一关键字不可动摇: 降级只发生在同价层内 (0 元降级后仍排在付费之前)。

跑法: python -m pytest tests/test_router_watermark_demote.py -v
"""
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from gateway.router import RouteEngine


@pytest.fixture
def engine(monkeypatch):
    """真实 config.settings 上 monkeypatch 水位/突发参数 (pytest 自动还原)。"""
    from config import settings
    monkeypatch.setattr(settings, "channel_5h_call_caps",
                        {"taotoken_coding": 2000}, raising=False)
    monkeypatch.setattr(settings, "watermark_demote_ratio", 0.6, raising=False)
    monkeypatch.setattr(settings, "watermark_restore_ratio", 0.4, raising=False)
    monkeypatch.setattr(settings, "burst_demote_channels", ["glm_bigmodel"],
                        raising=False)
    monkeypatch.setattr(settings, "burst_demote_threshold", 3, raising=False)
    monkeypatch.setattr(settings, "burst_demote_window_minutes", 60, raising=False)
    monkeypatch.setattr(settings, "burst_demote_minutes", 10, raising=False)
    return RouteEngine()


def _item(name, priority):
    """组路由候选条目 (channel, model_id, price, family_bonus, member_priority, channel_model)"""
    return ({"name": name, "priority": priority, "billing_mode": "subscription"},
            "glm-5.3-flash", 0.0, 0, 100, name)


def test_watermark_demote_reorders_within_tier(engine):
    """0 元层内: 被水位降级的渠道排到健康渠道之后 (base 顺序 tt 先)"""
    e = engine
    key_tt = e._dynamic_sort_key(_item("tt", 9))
    key_gb = e._dynamic_sort_key(_item("gb", 8))
    assert key_tt < key_gb

    e._watermark_demoted = {"tt"}
    key_tt = e._dynamic_sort_key(_item("tt", 9))
    key_gb = e._dynamic_sort_key(_item("gb", 8))
    assert key_gb < key_tt


def test_burst_demote_reorders_within_tier(engine):
    """429 突发软降级: 突发中的渠道同价层内排后"""
    e = engine
    e._burst_demote_until = {"gb": time.time() + 600}
    key_tt = e._dynamic_sort_key(_item("tt", 9))
    key_gb = e._dynamic_sort_key(_item("gb", 8))
    assert key_tt < key_gb


def test_demote_respects_price_tier():
    """降级只发生在同价层内: 降级的 0 元渠道仍排在付费 (0.5 元) 之前"""
    e = RouteEngine()
    demoted_free = _item("tt", 9)
    healthy_paid = ({"name": "ch1", "priority": 10, "billing_mode": "pay_per_token"},
                    "glm-5.3-flash", 0.5, 0, 100, "ch1")
    assert e._dynamic_sort_key(demoted_free) < e._dynamic_sort_key(healthy_paid)


def test_watermark_hysteresis(engine):
    """滞回: ≥60% 降级 → 40-60% 区间保持 → <40% 回升"""
    e = engine
    e._update_watermark_state({"taotoken_coding": 1300})
    assert "taotoken_coding" in e._watermark_demoted
    e._update_watermark_state({"taotoken_coding": 1000})
    assert "taotoken_coding" in e._watermark_demoted
    e._update_watermark_state({"taotoken_coding": 700})
    assert "taotoken_coding" not in e._watermark_demoted


def test_watermark_below_restore_ratio_no_demote(engine):
    """<40% 且本就未降级 → 幂等无变化"""
    e = engine
    e._update_watermark_state({"taotoken_coding": 700})
    assert "taotoken_coding" not in e._watermark_demoted


def test_burst_state_threshold_and_expiry(engine):
    """突发: ≥3 次触发并刷新截止; 阈值以下不清状态 (到期自然失效)"""
    e = engine
    e._update_burst_state({"glm_bigmodel": 3})
    assert time.time() < e._burst_demote_until.get("glm_bigmodel", 0)
    until_1 = e._burst_demote_until["glm_bigmodel"]
    e._update_burst_state({"glm_bigmodel": 5})
    assert e._burst_demote_until["glm_bigmodel"] > until_1
    e._update_burst_state({"glm_bigmodel": 2})
    assert "glm_bigmodel" in e._burst_demote_until


def test_burst_state_removes_unconfigured(engine):
    """V6.x 超时突发共用降级位后清理规则改为到期清理 (2026-11-04):
    未过期条目保留 (旧"滑出配置列表即清"规则会把不在 429 白名单里的
    超时降级在每轮刷新时误清), 已过期条目清理兜底"""
    e = engine
    # 未过期: 保留 (超时突发降级的渠道大多不在 429 白名单里)
    e._burst_demote_until = {"legacy_channel": time.time() + 600}
    e._update_burst_state({})
    assert "legacy_channel" in e._burst_demote_until
    # 已过期: 清理
    e._burst_demote_until["expired_channel"] = time.time() - 1
    e._update_burst_state({})
    assert "expired_channel" not in e._burst_demote_until
