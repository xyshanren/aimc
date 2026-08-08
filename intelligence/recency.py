"""V4.5.1 能力评分路由: 时间衰减工具函数

设计: 半线性衰减, 用于 model_cards.score 的时效折扣.
理由 (V4.5.1 design):
- 6 个月内 (180 天) → 1.0 (满分, 默认新鲜)
- 12 个月 (365 天) → 0.5
- 24 个月 (730 天) → 0.0
- > 24 个月 → 0.0 (太老, 不入路由)

公式: recency = max(0, 1 - (age_days / (2 * half_life_days)))
- half_life_days = 180 (半年半衰期)
- age = (now - updated_at).days

边界:
- updated_at 为 None → 0.0 (无时间戳不入路由)
- updated_at 在未来 → 1.0 (视为最新)
"""

from datetime import datetime, timezone
from typing import Optional


DEFAULT_HALF_LIFE_DAYS = 180


def age_days(updated_at: Optional[datetime], now: Optional[datetime] = None) -> Optional[float]:
    """距 updated_at 的天数 (小数). updated_at=None 时返 None."""
    if updated_at is None:
        return None
    if now is None:
        now = datetime.now(timezone.utc) if updated_at.tzinfo else datetime.now()
    delta = now - updated_at
    return delta.total_seconds() / 86400.0


def recency_weight(
    updated_at: Optional[datetime],
    half_life_days: int = DEFAULT_HALF_LIFE_DAYS,
    now: Optional[datetime] = None,
) -> float:
    """半线性衰减: [0, 1] 范围.

    Args:
        updated_at: 数据更新时间 (UTC-naive 或 tz-aware 都接)
        half_life_days: 半衰期 (默认 180 天 ≈ 半年)
        now: 注入的"当前时间" (测试用, 默认 datetime.now())

    Returns:
        [0.0, 1.0] 浮点权重. None 时间戳 → 0.0; 未来时间 → 1.0.

    Examples:
        >>> recency_weight(datetime.now())      # 刚刚 → 1.0
        >>> recency_weight(datetime.now() - timedelta(days=180))  # 半年前 → 0.5
        >>> recency_weight(datetime.now() - timedelta(days=360))  # 1 年前 → 0.0 (clamped)
        >>> recency_weight(None)                # 无时间戳 → 0.0
    """
    if updated_at is None:
        return 0.0
    age = age_days(updated_at, now)
    if age is None:
        return 0.0
    if age < 0:
        return 1.0  # 未来时间 → 视为最新
    weight = 1.0 - (age / (2.0 * half_life_days))
    return max(0.0, weight)