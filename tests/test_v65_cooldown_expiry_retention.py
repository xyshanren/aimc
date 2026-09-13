"""V6.5 批次一: 冷却过期保留 count — 修复指数退避被「过期 pop」架空.

背景 (v64x-429-fix-review.md §2.1):
- 旧行为: is_in_cooldown / cooldown_remaining 在冷却过期时 pop 整个 state,
  count 一起丢 → 现实主场景「到期 → router 候选过滤查询 → 重撞 429」下
  count 恒从 1 重来, 指数退避永远停在首档 30s。
- 新行为: 过期只置 cooldown_until=None 并记录 cooldown_expired_at,
  state 保留; 超过 _COUNT_RETENTION_SECONDS 保留窗才由 cleanup() 物理清理
  或 mark_failure 视作新的一系列失败。

全部用独立 CooldownManager 实例, 不触碰模块级单例 (测试隔离)。
"""

import time

from gateway.cooldown import CooldownManager
from gateway.error_classifier import ErrorCategory, ErrorInfo


def _rate_limited_info() -> ErrorInfo:
    return ErrorInfo(
        category=ErrorCategory.RATE_LIMITED,
        error_code="429001",
        error_msg="pool exhausted",
        needs_human=False,
        suggested_cooldown_seconds=30,
        suggested_probe_after_seconds=60,
    )


def _quota_info() -> ErrorInfo:
    return ErrorInfo(
        category=ErrorCategory.QUOTA_EXHAUSTED,
        error_code=None,
        error_msg="insufficient balance",
        needs_human=True,
        suggested_cooldown_seconds=3600,
        suggested_probe_after_seconds=None,
    )


def test_expiry_query_keeps_count_backoff_continues():
    """核心回归: 到期→查询→重撞 序列下退避必须升级 (旧行为恒 30s)。"""
    cm = CooldownManager()
    cm.mark_failure(1, error_info=_rate_limited_info())
    first_cd = cm._state[1]["cooldown_until"] - time.time()
    assert 29.5 <= first_cd <= 31

    # 冷却到期, router 候选过滤查询 (旧行为在此 pop 掉 count)
    cm._state[1]["cooldown_until"] = time.time() - 0.01
    assert cm.is_in_cooldown(1) is False
    assert 1 in cm._state, "过期查询后 state 必须保留 (count 供退避延续)"
    assert cm._state[1]["count"] == 1
    assert cm._state[1]["cooldown_until"] is None
    assert cm._state[1]["cooldown_expired_at"] is not None

    # 重撞: count 延续 → 第二档 60s
    cm.mark_failure(1, error_info=_rate_limited_info())
    assert cm._state[1]["count"] == 2
    second_cd = cm._state[1]["cooldown_until"] - time.time()
    assert 58 <= second_cd <= 61, f"第二档应为 60s, 实际 {second_cd:.1f}s"


def test_cooldown_remaining_expiry_keeps_state():
    cm = CooldownManager()
    cm.mark_failure(2, error_info=_rate_limited_info())
    cm._state[2]["cooldown_until"] = time.time() - 0.01
    assert cm.cooldown_remaining(2) == 0.0
    assert 2 in cm._state and cm._state[2]["count"] == 1


def test_success_after_expiry_query_full_reset():
    """过期后被查询再成功 → 全量复位 (含 cooldown_expired_at)。"""
    cm = CooldownManager()
    cm.mark_failure(3, error_info=_rate_limited_info())
    cm._state[3]["cooldown_until"] = time.time() - 0.01
    cm.is_in_cooldown(3)
    cm.mark_success(3)
    s = cm._state[3]
    assert s["count"] == 0
    assert s["cooldown_until"] is None
    assert s["cooldown_expired_at"] is None


def test_stale_count_reset_after_retention_window():
    """过期超过保留窗 → count 视为新系列, 不让陈年 count 吃满退避上限。"""
    cm = CooldownManager()
    cm.mark_failure(4, error_info=_rate_limited_info())
    cm.mark_failure(4, error_info=_rate_limited_info())
    assert cm._state[4]["count"] == 2
    # 模拟过期发生在 700s 前 (超过 600s 保留窗)
    cm._state[4]["cooldown_until"] = None
    cm._state[4]["cooldown_expired_at"] = time.time() - 700
    cm.mark_failure(4, error_info=_rate_limited_info())
    assert cm._state[4]["count"] == 1, "超保留窗后 count 应从 0 重来"
    cd = cm._state[4]["cooldown_until"] - time.time()
    assert 29 <= cd <= 31, f"新系列首档应 30s, 实际 {cd:.1f}s"


def test_within_retention_window_count_continues():
    cm = CooldownManager()
    cm.mark_failure(5, error_info=_rate_limited_info())
    cm._state[5]["cooldown_until"] = None
    cm._state[5]["cooldown_expired_at"] = time.time() - 100
    cm.mark_failure(5, error_info=_rate_limited_info())
    assert cm._state[5]["count"] == 2, "保留窗内 count 应延续"


def test_cleanup_keeps_recently_expired_cleans_old():
    cm = CooldownManager()
    cm.mark_failure(6, error_info=_rate_limited_info())
    cm._state[6]["cooldown_until"] = time.time() - 5
    cm._state[6]["next_probe_at"] = None  # rate_limited 会设 60s 后探活, 先清掉排除干扰
    cm.is_in_cooldown(6)  # 触发过期标记
    # 刚过期: cleanup 保留 (count 供退避延续)
    assert cm.cleanup() == 0
    assert 6 in cm._state
    # 过期超保留窗: cleanup 清掉
    cm._state[6]["cooldown_expired_at"] = time.time() - 700
    assert cm.cleanup() == 1
    assert 6 not in cm._state


def test_needs_human_survives_expiry_and_cleanup():
    """needs_human 不再被过期 pop 顺带洗白 — 洗白走 ack_recover / mark_success。"""
    cm = CooldownManager()
    # quota 的 needs_human 阈值 = 2 (needs_human_quota_threshold)
    cm.mark_failure(7, error_info=_quota_info())
    cm.mark_failure(7, error_info=_quota_info())
    assert cm.needs_human(7) is True
    cm._state[7]["cooldown_until"] = time.time() - 0.01
    cm.is_in_cooldown(7)
    assert cm.needs_human(7) is True, "过期查询不得清除 needs_human"
    assert cm.cleanup() == 0, "needs_human 永远保留, cleanup 不得清"


def test_realistic_ping_pong_sequence_escalates():
    """端到端复现生产乒乓场景: 429→冷却→到期→查询→重撞×3, 冷却必须逐级上升。"""
    cm = CooldownManager()
    cooldowns = []
    for _ in range(3):
        cm.mark_failure(8, error_info=_rate_limited_info())
        cooldowns.append(cm._state[8]["cooldown_until"] - time.time())
        # 冷却到期 + router 查询 (旧行为在此丢 count)
        cm._state[8]["cooldown_until"] = time.time() - 0.01
        cm.is_in_cooldown(8)
    assert cooldowns[0] < cooldowns[1] < cooldowns[2], (
        f"退避序列应递增: {[round(c) for c in cooldowns]}"
    )
