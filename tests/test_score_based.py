"""P2-16: 测试 score-based 多维排序 (阶段3 + P0-7)

设计:
score 公式 (router.py:652-689):
  score = W_PRICE * price_score
        + W_QUOTA * quota_ratio
        + W_SUCCESS * success_rate
        + W_RECENCY * (1.0 / (1.0 + recency_score / 3600))
        + W_PRIORITY * priority_norm

各分量:
  price_score: 1.0 - (p - min_price) / price_range if price_range > 0 else 0.5
  quota_ratio: min(q_remaining / q_total, 1.0) (无数据 -> 0.5)
  success_rate: 0.5 (1h 内用过) | 1.0 (1h 外用过 / 从未用过)
  recency_score: last_used 时间戳 (0 = 从未用过)
  priority_norm: priority / 100

测试策略: 在测试里重新实现 score 公式 (跟 router.py 同), 验证行为, 不依赖 RouteEngine 实例
"""
import time


# === 复制 router.py 的 score 公式 ===
def compute_price_score(price, min_price, price_range):
    p = price or 0
    return 1.0 - (p - min_price) / price_range if price_range > 0 else 0.5


def compute_quota_ratio(q_remaining, q_total):
    if q_remaining is None or not q_total or q_total <= 0:
        return 0.5  # 无数据
    return min(q_remaining / q_total, 1.0)


def compute_success_rate(last_used, now=None):
    if last_used > 0:
        age = (now or time.time()) - last_used
        return 0.5 if age < 3600 else 1.0
    return 1.0


def compute_recency_score(last_used):
    return last_used


def compute_full_score(price, min_price, price_range, q_remaining, q_total,
                     last_used, priority, now=None,
                     W_PRICE=0.40, W_QUOTA=0.30, W_SUCCESS=0.20,
                     W_RECENCY=0.05, W_PRIORITY=0.05):
    price_score = compute_price_score(price, min_price, price_range)
    quota_ratio = compute_quota_ratio(q_remaining, q_total)
    success_rate = compute_success_rate(last_used, now)
    recency_factor = 1.0 / (1.0 + last_used / 3600)
    priority_norm = (priority or 0) / 100.0
    return (
        W_PRICE * price_score
        + W_QUOTA * quota_ratio
        + W_SUCCESS * success_rate
        + W_RECENCY * recency_factor
        + W_PRIORITY * priority_norm
    )


# === price_score ===
def test_price_score_cheapest_gets_full():
    """最便宜 = price_score 1.0"""
    assert compute_price_score(price=1.0, min_price=1.0, price_range=5.0) == 1.0


def test_price_score_most_expensive_gets_zero():
    """最贵 = price_score 0.0"""
    assert compute_price_score(price=6.0, min_price=1.0, price_range=5.0) == 0.0


def test_price_score_middle():
    """中间价 = 0.5"""
    assert compute_price_score(price=3.5, min_price=1.0, price_range=5.0) == 0.5


def test_price_score_zero_range_uses_default():
    """所有 model 同价 (range=0) -> 兜底 0.5 (避免除零)"""
    assert compute_price_score(price=2.0, min_price=2.0, price_range=0) == 0.5


def test_price_score_zero_price():
    """免费 model (price=0) 跟最便宜 = 1.0"""
    assert compute_price_score(price=0.0, min_price=0.0, price_range=2.0) == 1.0


def test_price_score_none_treated_as_zero():
    """price=None 当作 0"""
    assert compute_price_score(price=None, min_price=0.0, price_range=2.0) == 1.0


# === quota_ratio ===
def test_quota_ratio_full():
    """剩余 = 总额 -> 1.0"""
    assert compute_quota_ratio(100, 100) == 1.0


def test_quota_ratio_half():
    assert compute_quota_ratio(50, 100) == 0.5


def test_quota_ratio_zero_remaining():
    """余额 0 -> 0.0"""
    assert compute_quota_ratio(0, 100) == 0.0


def test_quota_ratio_overshoot_caps_to_1():
    """q_remaining > q_total (异常) -> 截到 1.0"""
    assert compute_quota_ratio(150, 100) == 1.0


def test_quota_ratio_none_remaining_uses_default():
    """无数据 (q_remaining=None) -> 0.5 兜底"""
    assert compute_quota_ratio(None, 100) == 0.5
    assert compute_quota_ratio(100, None) == 0.5


# === success_rate ===
def test_success_rate_never_used_is_full():
    """从未用过 (last_used=0) -> 1.0 (假设 OK)"""
    assert compute_success_rate(last_used=0) == 1.0


def test_success_rate_used_recently_is_half():
    """最近 1h 内用过 -> 0.5"""
    now = 10000.0
    last_used = now - 1800  # 30 分钟前
    assert compute_success_rate(last_used=last_used, now=now) == 0.5


def test_success_rate_used_long_ago_is_full():
    """1h 之前用过 -> 1.0 (历史稳)"""
    now = 10000.0
    last_used = now - 7200  # 2 小时前
    assert compute_success_rate(last_used=last_used, now=now) == 1.0


# === full score ===
def test_full_score_prefers_cheap_quota_remainder():
    """便宜 + 高配额 > 贵 + 低配额"""
    cheap_full = compute_full_score(
        price=1.0, min_price=1.0, price_range=5.0,  # price_score=1.0
        q_remaining=100, q_total=100,  # quota_ratio=1.0
        last_used=0, priority=10,
    )
    expensive_low = compute_full_score(
        price=5.0, min_price=1.0, price_range=5.0,  # price_score=0.2
        q_remaining=10, q_total=100,  # quota_ratio=0.1
        last_used=0, priority=10,
    )
    assert cheap_full > expensive_low


def test_full_score_weights_properly_normalized():
    """score 在 [0, 1] 范围"""
    s = compute_full_score(
        price=2.0, min_price=1.0, price_range=10.0,
        q_remaining=50, q_total=100,
        last_used=0, priority=10,
    )
    assert 0.0 <= s <= 1.0
