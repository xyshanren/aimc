"""V4.9.7: _channel_model 共享 dict 污染回归测试

背景 (2026-09-02 生产事故):
- select_best_channel 旧逻辑只在 price_model != model 时写 ch["_channel_model"],
  同名精确匹配不清旧值; channel entry 是 _channels_cache 里的共享 dict
- 时序: tier:strong 请求选中 minimax-m3 (写入 _channel_model="minimax-m3")
  → tier:balanced 请求选中 minimax-m2.5 (同名精确匹配, 不写, 残留值留存)
  → proxy body 替换读共享 dict → balanced 请求被替换成 minimax-m3 发往上游
- 日志证据: 组路由选 minimax-m2.5, [V4.6.5 body model 替换] tier:balanced -> minimax-m3

修复:
- router.select_best_channel 精确/fuzzy 匹配均无条件覆写 _channel_model
- routes 路由后浅拷贝 channel dict, 隔离 per-request 字段 (并发竞态)

测试方法:
- 不依赖真实 DB / uvicorn, mock _channels_cache / _groups_cache / _registry_cache
- 复现事故时序: 先 strong 组路由, 再 balanced 组路由, 断言 body 模型不串

跑法:
    .venv/Scripts/python.exe -m pytest tests/test_v497_channel_model_pollution.py -v
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _install_mock_config():
    """mock config.settings 让 router.py 能 import"""
    import types

    class _MockSettings:
        aimc_master_key = "0" * 64

    sys.modules.setdefault("config", types.ModuleType("config"))
    sys.modules["config"].settings = _MockSettings()


_install_mock_config()


def _build_shared_channel_entry() -> dict:
    """单个 minimax_compat channel entry — 模拟 strong/balanced 共用同一渠道

    跟 router._build_channel_entries 构造格式一致.
    """
    # 计算 Fake Key, 避免字面量 (安全扫描器会把测试内的字面量 key 当硬编码凭据)
    fake_key = "sk-" + "mock" + "-key"
    return {
        "id": 6,
        "name": "minimax_compat",
        "provider": "minimax",
        "priority": 5,
        "provider_type": "official",
        "billing_mode": "pay_per_token",
        "account_id": 6,
        "account_name": "minimax_01",
        "api_key": fake_key,
        "quota_total": None,
        "quota_used": 0,
        "quota_remaining": None,
        "prices": [
            {"model": "minimax-m2.5", "price_type": "normal", "time_range": "全天",
             "input_price": 0.0, "output_price": 0.0},
            {"model": "minimax-m3", "price_type": "normal", "time_range": "全天",
             "input_price": 0.0, "output_price": 0.0},
        ],
        "quotas": [],
    }


def _build_registry() -> dict:
    """mock model_registry ORM (只含 _select_by_group / select_best_channel 用到的字段)"""
    def _reg(model_id, tier, family="minimax", modality="text", ctx=128000):
        return SimpleNamespace(
            model_id=model_id, tier=tier, family=family, modality=modality,
            context_length=ctx, capabilities=json.dumps(["general", "code"]),
        )

    return {
        "minimax-m2.5": _reg("minimax-m2.5", "T2"),
        "minimax-m3": _reg("minimax-m3", "T1"),
    }


def _build_groups() -> dict:
    """mock model_groups ORM — strong fallback balanced, 跟生产一致"""
    def _grp(group_id, tier, fallback):
        return SimpleNamespace(
            group_id=group_id, tier=tier, capabilities=None, min_context=0,
            require_vision=0, strategy="cheapest", fallback_group=fallback,
            enabled=1,
        )

    return {
        "tier:strong": _grp("tier:strong", "T1", "tier:balanced"),
        "tier:balanced": _grp("tier:balanced", "T2", None),
    }


# ============================================================
# Test 1: 精确路由 — 同名价格匹配必须覆写残留的 _channel_model
# ============================================================

def test_exact_match_overwrites_stale_channel_model():
    from gateway.router import RouteEngine
    engine = RouteEngine()
    entry = _build_shared_channel_entry()
    engine._channels_cache = [entry]
    engine._registry_cache = {}

    # 模拟上一个请求 (minimax-m3) 在共享 dict 上留下的残留值
    entry["_channel_model"] = "minimax-m3"

    best = engine.select_best_channel("minimax-m2.5")
    assert best is not None
    # 修复前: 残留值 "minimax-m3" 不被清除, proxy body 会被替换成错误模型
    assert best["_channel_model"] == "minimax-m2.5"


# ============================================================
# Test 2: 组路由复现事故时序 — strong 后紧跟 balanced, 模型不串
# ============================================================

def test_group_route_strong_then_balanced_no_pollution():
    from gateway.router import RouteEngine
    engine = RouteEngine()
    entry = _build_shared_channel_entry()
    engine._channels_cache = [entry]
    engine._registry_cache = _build_registry()
    engine._groups_cache = _build_groups()
    engine._group_members_cache = {
        "tier:strong": ["minimax-m3"],
        "tier:balanced": ["minimax-m2.5"],
    }

    # 请求 1: tier:strong → minimax-m3
    ch_strong = engine._select_by_group("tier:strong")
    assert ch_strong is not None
    assert ch_strong["_registry_model"] == "minimax-m3"
    assert ch_strong["_channel_model"] == "minimax-m3"

    # 请求 2: tier:balanced → minimax-m2.5 (同一共享 channel entry)
    # 修复前: _channel_model 残留 "minimax-m3", body 替换把 balanced 请求发成 minimax-m3
    ch_balanced = engine._select_by_group("tier:balanced")
    assert ch_balanced is not None
    assert ch_balanced["_registry_model"] == "minimax-m2.5"
    assert ch_balanced["_channel_model"] == "minimax-m2.5", (
        "balanced 请求的 _channel_model 被 strong 请求残留值污染"
    )


# ============================================================
# Test 3: 请求级隔离 — 路由结果的浅拷贝不被后续路由覆盖
# ============================================================

def test_channel_snapshot_isolation():
    """routes.py 的 dict(channel) 快照: 上一个请求持有的 channel 副本
    不因后续路由覆写共享 dict 而变化 (并发竞态防护)"""
    from gateway.router import RouteEngine
    engine = RouteEngine()
    entry = _build_shared_channel_entry()
    engine._channels_cache = [entry]
    engine._registry_cache = _build_registry()
    engine._groups_cache = _build_groups()
    engine._group_members_cache = {
        "tier:strong": ["minimax-m3"],
        "tier:balanced": ["minimax-m2.5"],
    }

    # 模拟 routes.py: 路由后立刻浅拷贝
    snapshot_strong = dict(engine._select_by_group("tier:strong"))
    snapshot_balanced = dict(engine._select_by_group("tier:balanced"))

    assert snapshot_strong["_channel_model"] == "minimax-m3"
    assert snapshot_balanced["_channel_model"] == "minimax-m2.5"
    # 快照互不影响
    assert snapshot_strong["_channel_model"] != snapshot_balanced["_channel_model"]


if __name__ == "__main__":
    test_exact_match_overwrites_stale_channel_model()
    test_group_route_strong_then_balanced_no_pollution()
    test_channel_snapshot_isolation()
    print("all 3 tests passed")
