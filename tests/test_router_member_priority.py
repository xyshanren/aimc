"""V4.6.4 (2026-08-14) Issue 3 sort_key member_priority tie-break 测试
V5.1 (2026-09-02): 成员优先级激活 — 改为直接测 router 真实排序函数

覆盖:
- sort_key tuple: (price, family_bonus, member_priority, -channel.priority)
- member_priority 越小越优先; subscription/free billing 也参与
- member_priority 不覆盖 price (价格仍第一序)
- V5.1 回归: member_priority 压过 channel.priority (激活前 glm_bigmodel
  pri=8 赢 0 元平价, strong 85% 流量默认打 glm-3 — 本文件就是防它复发)

测试不依赖 db, 直接构造 candidates 调 _group_cheapest_sort_key.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from gateway.router import _group_cheapest_sort_key


def _make_channel(name, priority=5, billing="pay_per_token", price=0.0):
    return {
        "name": name,
        "priority": priority,
        "billing_mode": billing,
        "price": price,
    }


def _candidate(ch, model_id, price, family_bonus, member_priority):
    # 跟 router._select_by_group 的 6 元组同构 (末位 channel_model 不参与排序)
    return (ch, model_id, price, family_bonus, member_priority, model_id)


def test_member_priority_breaks_tie():
    """Issue 3 修复验证: 同价同 channel 时, member_priority 小的优先"""
    minimax_compat = _make_channel("minimax_compat", priority=5, price=0.0)

    candidates = [
        _candidate(minimax_compat, "deepseek-v4-pro", 0.0, 1, 100),
        _candidate(minimax_compat, "minimax-m2.7",   0.0, 1, 120),
        _candidate(minimax_compat, "minimax-m3",     0.0, 1, 110),
    ]
    sorted_candidates = sorted(candidates, key=_group_cheapest_sort_key)
    assert sorted_candidates[0][1] == "deepseek-v4-pro", \
        f"应 deepseek-v4-pro (priority=100), 实际 {sorted_candidates[0][1]}"
    assert sorted_candidates[1][1] == "minimax-m3"
    assert sorted_candidates[2][1] == "minimax-m2.7"
    print(f"  [OK] member_priority tie-break: deepseek-v4-pro 排第一 (priority=100)")


def test_subscription_free_member_priority_still_works():
    """subscription/free billing 也用 member_priority"""
    sub_channel = _make_channel("subscription_ch", priority=5, billing="subscription", price=0.0)
    candidates = [
        _candidate(sub_channel, "model_a", 0.0, 1, 200),  # priority 高 (劣)
        _candidate(sub_channel, "model_b", 0.0, 1, 100),  # priority 低 (优)
    ]
    sorted_candidates = sorted(candidates, key=_group_cheapest_sort_key)
    assert sorted_candidates[0][1] == "model_b"  # priority=100 优先
    print(f"  [OK] subscription billing 也走 member_priority tie-break")


def test_member_priority_does_not_override_price():
    """价格高的 member 即使 priority=100 也不应该被选中"""
    expensive = _make_channel("expensive_ch", priority=5, price=10.0)
    cheap = _make_channel("cheap_ch", priority=5, price=1.0)

    candidates = [
        _candidate(expensive, "model_a", 10.0, 1, 100),  # 高优先级但贵
        _candidate(cheap,     "model_b", 1.0,  1, 200),  # 低优先级但便宜
    ]
    sorted_candidates = sorted(candidates, key=_group_cheapest_sort_key)
    assert sorted_candidates[0][1] == "model_b", \
        f"应 model_b (便宜), 实际 {sorted_candidates[0][1]}"
    print(f"  [OK] member_priority 不覆盖 price: model_b (便宜) 排第一")


def test_member_priority_default_100():
    """member_priority 缺省 100 (跟 ORM default 一致)"""
    channel = _make_channel("ch", priority=5, price=0.0)
    candidates = [
        _candidate(channel, "model_a", 0.0, 1, 100),
        _candidate(channel, "model_b", 0.0, 1, 100),
    ]
    sorted_candidates = sorted(candidates, key=_group_cheapest_sort_key)
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
        _candidate(ch, "minimax-m2.7", 0.0, 1, 120),
        _candidate(ch, "minimax-m3",   0.0, 1, 100),
    ]
    sorted_candidates = sorted(candidates_in_sql_order, key=_group_cheapest_sort_key)
    # 修复后: m3 (priority=100) 排第一
    assert sorted_candidates[0][1] == "minimax-m3", \
        f"Issue 3 修复后应 minimax-m3, 实际 {sorted_candidates[0][1]}"
    print(f"  [OK] Issue 3 修复: m3 (priority=100) 排 m2.7 (priority=120) 前")


def test_member_priority_beats_channel_priority():
    """V5.1 激活回归: member_priority 压过 channel.priority

    激活前 (price, family_bonus, -channel.priority): glm-5.3-flash 走
    glm_bigmodel (pri=8) 反客为主变 strong 默认, T2 领跑全 T1 成员。
    激活后 member_priority 在 channel.priority 之前 — glm-5.2 (108, pri=5)
    应排 glm-5.3-flash (120, pri=8) 前。
    """
    sensenova = _make_channel("sensenova_official", priority=5, price=0.0)
    glm_bigmodel = _make_channel("glm_bigmodel", priority=8, billing="subscription", price=0.0)

    candidates = [
        _candidate(glm_bigmodel, "glm-5.3-flash",     0.0, 1, 120),  # 渠道 pri 高但成员 pri 低
        _candidate(sensenova,    "glm-5.2",           0.0, 1, 108),
        _candidate(sensenova,    "deepseek-v4-flash", 0.0, 1, 100),
    ]
    sorted_candidates = sorted(candidates, key=_group_cheapest_sort_key)
    order = [c[1] for c in sorted_candidates]
    assert order == ["deepseek-v4-flash", "glm-5.2", "glm-5.3-flash"], \
        f"V5.1 激活后应按成员优先级排序, 实际 {order}"
    print(f"  [OK] V5.1: member_priority 压过 channel.priority → {order}")


# ============================================================
# 主入口
# ============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("V5.1 成员优先级激活 — _group_cheapest_sort_key 测试")
    print("=" * 60)

    print("\n=== member_priority tie-break ===")
    test_member_priority_breaks_tie()
    test_subscription_free_member_priority_still_works()
    test_member_priority_does_not_override_price()
    test_member_priority_default_100()
    test_member_priority_breaks_stable_sort_issue3()
    test_member_priority_beats_channel_priority()

    print("\n" + "=" * 60)
    print("ALL TESTS PASSED")
    print("=" * 60)
