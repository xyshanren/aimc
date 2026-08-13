"""NULL 价格路由排序测试 (Phase 2a+ fix, 2026-08-13)

背景:
- refresh_siliconflow_models.py 把硅基流动 90+ model 名以 NULL 价格入库 price_snapshots
- 修复前 router.py:426 sort_key 用 `price.get("input_price") or 0` 把 NULL 当 0 元,
  导致硅基流动 NULL 价格 model 在精确路由里永远排第一 (误选)
- 修复后 NULL → 999 兜底, 跟组路由 _get_input_price 对齐

场景:
- NULL 价格应排最后 (999 兜底), 而不是排第一 (修复前的 0 元 bug)
- 有真实价格的 channel 应优先选
- NULL 价格是唯一候选时 (兜底场景), 应胜出

测试方法:
- 不依赖真实 DB / uvicorn
- 直接构造 RouteEngine._channels_cache 内存 mock, 调 select_best_channel
- 验证选中的 channel name + 排序逻辑

跑法:
    .venv/Scripts/python.exe tests/test_null_price_routing.py
"""

import sys
from pathlib import Path

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


def _build_channels_cache(scenario_name: str) -> list[dict]:
    """构造 mock _channels_cache, 模拟 refresh_siliconflow_models 入库后的场景.

    返回 channel dict 列表 (跟 router.py:_build_channel_entries 构造的格式一致).
    """
    if scenario_name == "three_channels":
        # 场景 1: 3 个 channel, 1 个 NULL + 2 个有真实价格
        return [
            {
                "id": 1,
                "name": "siliconflow_aggregator",
                "provider": "siliconflow",
                "priority": 1,
                "provider_type": "aggregator",
                "billing_mode": "pay_per_token",
                "account_id": 1,
                "account_name": "sf-main",
                "api_key": "sk-mock",
                "quota_total": None,
                "quota_used": 0,
                "quota_remaining": None,
                "prices": [
                    # NULL 价格 (refresh_siliconflow_models 入库)
                    {"model": "test-model", "price_type": "normal", "time_range": "全天",
                     "input_price": None, "output_price": None},
                ],
                "quotas": [],
            },
            {
                "id": 2,
                "name": "deepseek_official",
                "provider": "deepseek",
                "priority": 10,
                "provider_type": "official",
                "billing_mode": "pay_per_token",
                "account_id": 2,
                "account_name": "ds-main",
                "api_key": "sk-mock",
                "quota_total": None,
                "quota_used": 0,
                "quota_remaining": None,
                "prices": [
                    {"model": "test-model", "price_type": "normal", "time_range": "全天",
                     "input_price": 4.0, "output_price": 16.0},
                ],
                "quotas": [],
            },
            {
                "id": 3,
                "name": "qwen_bailian",
                "provider": "qwen",
                "priority": 5,
                "provider_type": "official",
                "billing_mode": "pay_per_token",
                "account_id": 3,
                "account_name": "qw-main",
                "api_key": "sk-mock",
                "quota_total": None,
                "quota_used": 0,
                "quota_remaining": None,
                "prices": [
                    {"model": "test-model", "price_type": "normal", "time_range": "全天",
                     "input_price": 0.8, "output_price": 2.0},
                ],
                "quotas": [],
            },
        ]
    elif scenario_name == "null_only":
        # 场景 2: 只有硅基流动 NULL 价格 (唯一候选, 兜底场景)
        return [
            {
                "id": 1,
                "name": "siliconflow_aggregator",
                "provider": "siliconflow",
                "priority": 1,
                "provider_type": "aggregator",
                "billing_mode": "pay_per_token",
                "account_id": 1,
                "account_name": "sf-main",
                "api_key": "sk-mock",
                "quota_total": None,
                "quota_used": 0,
                "quota_remaining": None,
                "prices": [
                    {"model": "test-model", "price_type": "normal", "time_range": "全天",
                     "input_price": None, "output_price": None},
                ],
                "quotas": [],
            },
        ]
    elif scenario_name == "real_price_vs_null":
        # 场景 3: NULL vs 真实价格 (1 元)
        return [
            {
                "id": 1,
                "name": "siliconflow_aggregator",
                "provider": "siliconflow",
                "priority": 1,
                "provider_type": "aggregator",
                "billing_mode": "pay_per_token",
                "account_id": 1,
                "account_name": "sf-main",
                "api_key": "sk-mock",
                "quota_total": None,
                "quota_used": 0,
                "quota_remaining": None,
                "prices": [
                    {"model": "test-model", "price_type": "normal", "time_range": "全天",
                     "input_price": None, "output_price": None},
                ],
                "quotas": [],
            },
            {
                "id": 2,
                "name": "deepseek_official",
                "provider": "deepseek",
                "priority": 10,
                "provider_type": "official",
                "billing_mode": "pay_per_token",
                "account_id": 2,
                "account_name": "ds-main",
                "api_key": "sk-mock",
                "quota_total": None,
                "quota_used": 0,
                "quota_remaining": None,
                "prices": [
                    {"model": "test-model", "price_type": "normal", "time_range": "全天",
                     "input_price": 1.0, "output_price": 2.0},
                ],
                "quotas": [],
            },
        ]
    else:
        raise ValueError(f"unknown scenario: {scenario_name}")


def _run_select_best(scenario_name: str) -> dict | None:
    """注入 mock channels cache, 跑 RouteEngine.select_best_channel, 返回 channel dict"""
    from gateway.router import RouteEngine
    engine = RouteEngine()
    engine._channels_cache = _build_channels_cache(scenario_name)
    engine._registry_cache = {}  # 不影响测试
    return engine.select_best_channel("test-model")


# ============================================================
# Test 1: NULL 价格排最后 (有真实价格时, NULL 不应胜出)
# ============================================================
def test_null_price_ranks_last():
    """场景: 硅基流动 NULL + deepseek 4/16 + qwen 0.8/2
    期望: qwen_bailian 胜出 (最便宜 0.8), 而不是硅基流动 (NULL 价但修复前被当 0 元)
    """
    print("\n=== Test 1: NULL 价格排最后 (3 channel 候选) ===")
    channel = _run_select_best("three_channels")
    assert channel is not None, "应能选到 channel"
    print(f"  选中: {channel['name']!r} (input_price={channel['prices'][0]['input_price']})")
    assert channel["name"] == "qwen_bailian", \
        f"应选最便宜的 qwen (0.8 元), 实际选 {channel['name']!r}"
    print(f"  [OK] NULL 价格 (硅基流动) 排最后, qwen 0.8 胜出")


# ============================================================
# Test 2: NULL 价格是唯一候选时 (兜底场景, NULL 应胜出)
# ============================================================
def test_null_price_only_candidate():
    """场景: 只有硅基流动 NULL 价格
    期望: 硅基流动胜出 (唯一候选, 是聚合平台的兜底 channel)

    注: 此场景下 _get_current_price 找到 NULL 价 price_info 进入候选,
    不会走 _get_fallback_channel 兜底路径 (因为候选非空).
    """
    print("\n=== Test 2: NULL 价格是唯一候选 (兜底胜出) ===")
    channel = _run_select_best("null_only")
    assert channel is not None, "NULL 唯一候选应胜出 (兜底)"
    print(f"  选中: {channel['name']!r}")
    assert channel["name"] == "siliconflow_aggregator", \
        f"唯一候选 NULL 应胜出, 实际 {channel['name']!r}"
    print(f"  [OK] NULL 唯一候选 → 硅基流动兜底胜出")


# ============================================================
# Test 3: NULL 价格 vs 真实价格 (NULL 应排最后)
# ============================================================
def test_real_price_beats_null():
    """场景: 硅基流动 NULL + deepseek_official 1/2
    期望: deepseek_official 胜出 (1 元 vs NULL 999 元)
    """
    print("\n=== Test 3: 真实价格 1 元 vs NULL 价格 ===")
    channel = _run_select_best("real_price_vs_null")
    assert channel is not None, "应能选到 channel"
    print(f"  选中: {channel['name']!r} (input_price={channel['prices'][0]['input_price']})")
    assert channel["name"] == "deepseek_official", \
        f"应选 deepseek (1 元), 实际选 {channel['name']!r}"
    print(f"  [OK] 真实价格 1 元排第一, NULL 999 元排最后")


# ============================================================
# Main
# ============================================================
def main():
    print("=" * 60)
    print("NULL 价格路由排序测试 (Phase 2a+ fix, 2026-08-13)")
    print("=" * 60)
    print("覆盖 3 个场景: NULL 排最后 / NULL 唯一候选 / NULL vs 真实价")

    test_null_price_ranks_last()
    test_null_price_only_candidate()
    test_real_price_beats_null()

    print("\n" + "=" * 60)
    print("ALL 3 TESTS PASSED")
    print("=" * 60)
    print()
    print("结论:")
    print("- Phase 2a+ fix: router.py:426 sort_key NULL → 999 兜底")
    print("- 跟组路由 _get_input_price (router.py:956-964) 行为一致")
    print("- 硅基流动 NULL 价格 model 不再 '0 元误中'")


if __name__ == "__main__":
    main()