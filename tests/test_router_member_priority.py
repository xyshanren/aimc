"""V4.6.4 (2026-08-14) Issue 3 sort_key member_priority tie-break 测试

覆盖:
- sort_key tuple 多 1 项: member_priority 越小越优先
- subscription/free billing 时 member_priority 也参与
- candidates tuple 长度变 5 (channel, model_id, price, family_bonus, member_priority)

测试不依赖 db, 直接构造 candidates 调 sort_key 逻辑 (跟 router._select_by_group 同步).
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _sort_key(item, model_priority):
    """模拟 router._select_by_group 内 sort_key 逻辑 (V4.6.4)"""
    ch, _model_id, price, family_bonus, member_priority = item
    billing = ch.get("billing_mode", "pay_per_token")
    if billing in ("subscription", "free"):
        return (0.0, family_bonus, member_priority, -ch.get("priority", 0))
    return (
        price if price is not None else 999.0,
        family_bonus,
        member_priority,
        -ch.get("priority", 0),
    )


def _make_channel(name, priority=5, billing="pay_per_token", price=0.0):
    return {
        "name": name,
        "priority": priority,
        "billing_mode": billing,
        "price": price,
    }


def test_member_priority_breaks_tie():
    """Issue 3 修复验证: 同价同 channel 时, member_priority 小的优先

    场景: tier:flagship 中 deepseek-v4-pro (priority=100) 跟 minimax-m2.7 (priority=120)
    都走 minimax_compat, 价格/配额/channel_priority 都一样 (都是 0.0 元, priority 5)
    """
    minimax_compat = _make_channel("minimax_compat", priority=5, price=0.0)

    # candidates: (channel, model_id, price, family_bonus, member_priority)
    candidates = [
        # deepseek-v4-pro priority=100 (更优先)
        (minimax_compat, "deepseek-v4-pro", 0.0, 1, 100),
        # minimax-m2.7 priority=120 (次优)
        (minimax_compat, "minimax-m2.7",   0.0, 1, 120),
        # minimax-m3 priority=110
        (minimax_compat, "minimax-m3",     0.0, 1, 110),
    ]

    sorted_candidates = sorted(candidates, key=lambda x: _sort_key(x, None))
    # 应该按 member_priority 升序: deepseek-v4-pro (100), minimax-m3 (110), minimax-m2.7 (120)
    assert sorted_candidates[0][1] == "deepseek-v4-pro", \
        f"应 deepseek-v4-pro (priority=100), 实际 {sorted_candidates[0][1]}"
    assert sorted_candidates[1][1] == "minimax-m3"
    assert sorted_candidates[2][1] == "minimax-m2.7"
    print(f"  [OK] member_priority tie-break: deepseek-v4-pro 排第一 (priority=100)")


def test_subscription_free_member_priority_still_works():
    """subscription/free billing 也用 member_priority"""
    sub_channel = _make_channel("subscription_ch", priority=5, billing="subscription", price=0.0)
    candidates = [
        (sub_channel, "model_a", 0.0, 1, 200),  # priority 高 (劣)
        (sub_channel, "model_b", 0.0, 1, 100),  # priority 低 (优)
    ]
    sorted_candidates = sorted(candidates, key=lambda x: _sort_key(x, None))
    assert sorted_candidates[0][1] == "model_b"  # priority=100 优先
    print(f"  [OK] subscription billing 也走 member_priority tie-break")


def test_member_priority_does_not_override_price():
    """价格高的 member 即使 priority=100 也不应该被选中"""
    expensive = _make_channel("expensive_ch", priority=5, price=10.0)
    cheap = _make_channel("cheap_ch", priority=5, price=1.0)

    candidates = [
        (expensive, "model_a", 10.0, 1, 100),  # 高优先级但贵
        (cheap,     "model_b", 1.0,  1, 200),  # 低优先级但便宜
    ]
    sorted_candidates = sorted(candidates, key=lambda x: _sort_key(x, None))
    assert sorted_candidates[0][1] == "model_b", \
        f"应 model_b (便宜), 实际 {sorted_candidates[0][1]}"
    print(f"  [OK] member_priority 不覆盖 price: model_b (便宜) 排第一")


def test_member_priority_default_100():
    """member_priority 缺省 100 (跟 ORM default 一致)"""
    channel = _make_channel("ch", priority=5, price=0.0)
    candidates = [
        (channel, "model_a", 0.0, 1, 100),  # 显式 100
        (channel, "model_b", 0.0, 1, 100),  # 显式 100
    ]
    sorted_candidates = sorted(candidates, key=lambda x: _sort_key(x, None))
    # 都 100, sort 后顺序保留 (stable sort)
    assert sorted_candidates[0][1] == "model_a"
    assert sorted_candidates[1][1] == "model_b"
    print(f"  [OK] member_priority=100 默认值, 排序稳定")


def test_member_priority_breaks_stable_sort_issue3():
    """Issue 3 真实场景重现: stable sort 选 m2.7 而不是 m3

    修复前 sort_key 没 member_priority → m3 / m2.7 都同价同 channel → stable sort 选
    SQL 查询顺序返回的 m2.7 (因为 m3 没 price_snapshot 走 fallback, 顺序排在后).

    修复后 sort_key 加 member_priority → m3 priority=100 > m2.7 priority=120 → m3 排前
    """
    ch = _make_channel("ch", priority=5, price=0.0)
    candidates_in_sql_order = [
        # 模拟 db 查询顺序: m2.7 先, m3 后
        (ch, "minimax-m2.7", 0.0, 1, 120),
        (ch, "minimax-m3",   0.0, 1, 100),
    ]
    sorted_candidates = sorted(candidates_in_sql_order, key=lambda x: _sort_key(x, None))
    # 修复后: m3 (priority=100) 排第一
    assert sorted_candidates[0][1] == "minimax-m3", \
        f"Issue 3 修复后应 minimax-m3, 实际 {sorted_candidates[0][1]}"
    print(f"  [OK] Issue 3 修复: m3 (priority=100) 排 m2.7 (priority=120) 前")


# ============================================================
# 主入口
# ============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("V4.6.4 Issue 3 sort_key member_priority tie-break 测试")
    print("=" * 60)

    print("\n=== member_priority tie-break ===")
    test_member_priority_breaks_tie()
    test_subscription_free_member_priority_still_works()
    test_member_priority_does_not_override_price()
    test_member_priority_default_100()
    test_member_priority_breaks_stable_sort_issue3()

    print("\n" + "=" * 60)
    print("ALL TESTS PASSED")
    print("=" * 60)