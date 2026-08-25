"""V4.9 (2026-08-25): CooldownManager 状态机测试

覆盖:
- 5 个状态迁移 (healthy/failing/degraded/needs_human/manual_disabled)
- needs_human 阈值 (quota/auth/model)
- 指数退避 + 探活间隔
- 人工禁用/启用
- 向后兼容 (旧 reason 字符串调用)
- snapshot 扩展字段

跑法:
    python tests/test_cooldown_state_machine.py
"""
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from gateway.cooldown import CooldownManager
from gateway.error_classifier import ErrorCategory, ErrorInfo, classify


_passed = 0
_failed = 0


def check(desc, got, expected):
    global _passed, _failed
    if got == expected:
        _passed += 1
        print(f"  PASS  {desc}")
    else:
        _failed += 1
        print(f"  FAIL  {desc}  got={got!r}  expected={expected!r}")


def make_quota_error():
    return ErrorInfo(
        category=ErrorCategory.QUOTA_EXHAUSTED,
        error_code="1113",
        error_msg="余额不足",
        needs_human=True,
        suggested_cooldown_seconds=3600,
        suggested_probe_after_seconds=1800,
    )


def make_auth_error():
    return ErrorInfo(
        category=ErrorCategory.AUTH_FAILED,
        error_code="invalid_api_key",
        error_msg="Invalid API key",
        needs_human=True,
        suggested_cooldown_seconds=86400,
        suggested_probe_after_seconds=None,
    )


def make_5xx_error():
    return ErrorInfo(
        category=ErrorCategory.UPSTREAM_5XX,
        error_code=None,
        error_msg="Internal Server Error",
        needs_human=False,
        suggested_cooldown_seconds=60,
        suggested_probe_after_seconds=300,
    )


def make_network_error():
    return ErrorInfo(
        category=ErrorCategory.NETWORK_TRANSIENT,
        error_code=None,
        error_msg="timeout",
        needs_human=False,
        suggested_cooldown_seconds=15,
        suggested_probe_after_seconds=60,
    )


# ===== 1. healthy → failing =====
def test_healthy_to_failing():
    """无 state → mark_failure → failing (cooldown_until 未来)"""
    cm = CooldownManager()
    cm.mark_failure(1, error_info=make_5xx_error())
    s = cm.snapshot()[1]
    check("count=1", s["count"], 1)
    check("in_cooldown", s["in_cooldown"], True)
    check("needs_human=False", s["needs_human"], False)
    check("last_error_type=upstream_5xx", s["last_error_type"], "upstream_5xx")
    check("next_probe_at 已设置", s["next_probe_at"] is not None, True)


def test_mark_success_resets_to_healthy():
    """failing → mark_success → healthy (count=0, in_cooldown=False)"""
    cm = CooldownManager()
    cm.mark_failure(1, error_info=make_5xx_error())
    assert cm.snapshot()[1]["in_cooldown"] is True
    cm.mark_success(1)
    s = cm.snapshot()[1]
    check("count=0", s["count"], 0)
    check("in_cooldown=False", s["in_cooldown"], False)
    check("last_success_at 已设置", s["last_success_at"] is not None, True)


# ===== 2. failing → degraded (探活失败但非 needs_human) =====
def test_failing_to_degraded():
    """needs_human=False 失败 → next_probe_at 已设置, 但 needs_human=False"""
    cm = CooldownManager()
    cm.mark_failure(1, error_info=make_network_error())  # cooldown 15s
    s = cm.snapshot()[1]
    check("needs_human=False", s["needs_human"], False)
    check("next_probe_at 已设置", s["next_probe_at"] is not None, True)
    check("probe_after ~60s", abs(s["next_probe_in_seconds"] - 60) < 5, True)


# ===== 3. failing → needs_human (quota 阈值=2) =====
def test_quota_2_failures_needs_human():
    """quota 失败 2 次 → needs_human=True"""
    cm = CooldownManager()
    cm.mark_failure(1, error_info=make_quota_error())
    s1 = cm.snapshot()[1]
    check("第1次 needs_human=False", s1["needs_human"], False)
    cm.mark_failure(1, error_info=make_quota_error())
    s2 = cm.snapshot()[1]
    check("第2次 needs_human=True", s2["needs_human"], True)
    check("next_probe_at=None (人工)", s2["next_probe_at"], None)


def test_auth_1_failure_needs_human():
    """auth 失败 1 次 → needs_human=True (auth 阈值=1)"""
    cm = CooldownManager()
    cm.mark_failure(1, error_info=make_auth_error())
    s = cm.snapshot()[1]
    check("1次 needs_human=True", s["needs_human"], True)
    check("next_probe_at=None", s["next_probe_at"], None)


# ===== 4. 5xx 永不 needs_human =====
def test_5xx_never_needs_human():
    """5xx 失败 100 次都不该 needs_human"""
    cm = CooldownManager()
    for _ in range(100):
        cm.mark_failure(1, error_info=make_5xx_error())
    s = cm.snapshot()[1]
    check("count=100", s["count"], 100)
    check("needs_human=False", s["needs_human"], False)


# ===== 5. 人工禁用 =====
def test_manual_disable_excludes_from_probe():
    """manual_disabled 后, 不主动探活"""
    cm = CooldownManager()
    cm.mark_failure(1, error_info=make_5xx_error())
    cm.mark_manual_disabled(1)
    s = cm.snapshot()[1]
    check("manual_disabled=True", s["manual_disabled"], True)
    check("in_cooldown=True (远期)", s["in_cooldown"], True)
    check("不在探活列表", 1 in cm.accounts_due_for_probe(), False)


def test_manual_disable_then_enable():
    """manual_disable → clear_manual_disable"""
    cm = CooldownManager()
    cm.mark_manual_disabled(1)
    assert cm.is_manual_disabled(1) is True
    cm.clear_manual_disabled(1)
    check("clear 后非 manual", cm.is_manual_disabled(1), False)


# ===== 6. 探活调度查询 =====
def test_accounts_due_for_probe():
    """now 跨越 next_probe_at → 加入 due_for_probe 列表"""
    cm = CooldownManager(cooldown_seconds=0)  # 让 cooldown 立即过期
    cm.mark_failure(1, error_info=make_network_error())  # probe_after=60
    # cooldown 已过期 (0s), 但 next_probe_at 在 60s 后 → 暂时不该探
    due = cm.accounts_due_for_probe()
    check("未到期不在 due", 1 in due, False)
    # 强制到 61s 后
    future = time.time() + 61
    due = cm.accounts_due_for_probe(now=future)
    check("到期进入 due", 1 in due, True)


def test_needs_human_not_due_for_probe():
    """needs_human=True 不在 due_for_probe 列表"""
    cm = CooldownManager()
    cm.mark_failure(1, error_info=make_auth_error())  # 1 次就 needs_human
    future = time.time() + 99999
    due = cm.accounts_due_for_probe(now=future)
    check("needs_human 不探活", 1 in due, False)


def test_accounts_needing_human():
    """accounts_needing_human 列出 needs_human=True 的"""
    cm = CooldownManager()
    cm.mark_failure(1, error_info=make_auth_error())  # needs_human
    cm.mark_failure(2, error_info=make_quota_error())
    cm.mark_failure(2, error_info=make_quota_error())  # 2 次 → needs_human
    cm.mark_failure(3, error_info=make_5xx_error())  # 不 needs_human
    n = sorted(cm.accounts_needing_human())
    check("needs_human = [1, 2]", n, [1, 2])


# ===== 7. probe_result =====
def test_probe_result_success_clears_state():
    """探活成功 → mark_success → 清 needs_human/next_probe_at"""
    cm = CooldownManager()
    cm.mark_failure(1, error_info=make_auth_error())
    assert cm.needs_human(1) is True
    cm.probe_result(1, success=True)
    s = cm.snapshot()[1]
    check("needs_human=False", s["needs_human"], False)
    check("in_cooldown=False", s["in_cooldown"], False)


def test_probe_result_failure_increments_probe_attempts():
    """探活失败 → probe_attempts += 1 + 再次 mark_failure"""
    cm = CooldownManager()
    cm.mark_failure(1, error_info=make_5xx_error())
    s_before = cm.snapshot()[1]
    pa_before = s_before["probe_attempts"]
    cm.probe_result(1, success=False, error_info=make_5xx_error())
    s_after = cm.snapshot()[1]
    check("probe_attempts += 1", s_after["probe_attempts"], pa_before + 1)
    check("count += 1", s_after["count"], s_before["count"] + 1)


def test_probe_result_failure_triggers_needs_human():
    """探活失败累计次数达阈值 → needs_human"""
    cm = CooldownManager()
    # 第 1 次: quota 失败 (count=1, 不 needs_human)
    cm.mark_failure(1, error_info=make_quota_error())
    # 第 2 次: 模拟是探活失败触发的
    cm.probe_result(1, success=False, error_info=make_quota_error())
    s = cm.snapshot()[1]
    check("count=2", s["count"], 2)
    check("needs_human=True (quota 阈值=2)", s["needs_human"], True)


# ===== 8. 指数退避 =====
def test_exponential_backoff():
    """连续失败后, 探活间隔指数增长"""
    cm = CooldownManager(cooldown_seconds=0, max_probe_backoff_seconds=3600)
    cm.mark_failure(1, error_info=make_5xx_error())  # probe_after=300, attempts=0 → 300s
    s1 = cm.snapshot()[1]
    base_after = 300
    check("第1次 ~300s", abs(s1["next_probe_in_seconds"] - base_after) < 5, True)

    # 模拟探活失败 (probe_attempts=1)
    cm.probe_result(1, success=False, error_info=make_5xx_error())
    s2 = cm.snapshot()[1]
    # 第2次: max(300, 300 * 2^0) = 300 (backoff 在 probe_attempts>0 后生效)
    check("第2次 ~300s", abs(s2["next_probe_in_seconds"] - base_after) < 5, True)

    # 第3次: attempts=2, max(300, 300 * 2^1) = 600
    cm.probe_result(1, success=False, error_info=make_5xx_error())
    s3 = cm.snapshot()[1]
    check("第3次 ~600s", abs(s3["next_probe_in_seconds"] - 600) < 5, True)


def test_backoff_capped():
    """指数退避不会超过 max_probe_backoff_seconds (多次探活失败后)"""
    cm = CooldownManager(cooldown_seconds=0, max_probe_backoff_seconds=100)
    cm.mark_failure(1, error_info=make_5xx_error())  # probe_after=300
    cm.probe_result(1, success=False, error_info=make_5xx_error())  # attempts=1
    cm.probe_result(1, success=False, error_info=make_5xx_error())  # attempts=2 → 翻倍
    s = cm.snapshot()[1]
    # attempts=2: backoff = min(100, 300 * 2^(2-1)) = min(100, 600) = 100
    check("backoff cap 100s", s["next_probe_in_seconds"] <= 105, True)


# ===== 9. 向后兼容 (旧 reason 字符串调用) =====
def test_backward_compat_reason_string():
    """旧调用方式 (reason 字符串) 仍能工作, 不报错"""
    cm = CooldownManager()
    cm.mark_failure(1, reason="upstream_500")  # 只传 reason, 不传 error_info
    s = cm.snapshot()[1]
    check("count=1 (旧路径)", s["count"], 1)
    check("in_cooldown=True", s["in_cooldown"], True)
    check("last_error_type 空 (旧路径)", s["last_error_type"], "")
    check("needs_human=False", s["needs_human"], False)


def test_backward_compat_reset():
    """旧 API reset() 仍能清空 state"""
    cm = CooldownManager()
    cm.mark_failure(1, reason="upstream_500")
    cm.reset(1)
    check("reset 后 state 不存在", 1 in cm.snapshot(), False)


# ===== 10. snapshot 扩展字段 =====
def test_snapshot_full_fields():
    """snapshot 返回所有 V4.9 字段"""
    cm = CooldownManager()
    cm.mark_failure(1, error_info=make_quota_error())
    s = cm.snapshot()[1]
    expected_keys = {
        "count", "cooldown_until", "remaining_seconds", "in_cooldown",
        "first_failure_at", "last_failure_at", "last_success_at",
        "last_error_type", "last_error_code", "last_error_msg",
        "needs_human", "next_probe_at", "next_probe_in_seconds",
        "probe_attempts", "manual_disabled",
    }
    actual_keys = set(s.keys())
    check(f"所有 V4.9 字段都存在 (missing: {expected_keys - actual_keys})",
          expected_keys - actual_keys, set())


# ===== 11. cleanup 行为 =====
def test_cleanup_keeps_needs_human():
    """cleanup 不会清掉 needs_human=True 的 entry"""
    cm = CooldownManager(cooldown_seconds=0)  # cooldown 立即过期
    cm.mark_failure(1, error_info=make_auth_error())  # needs_human=True
    cleaned = cm.cleanup()
    check("needs_human 不被 cleanup", 1 in cm.snapshot(), True)
    check("cleaned=0", cleaned, 0)


def test_cleanup_keeps_manual_disabled():
    """cleanup 不会清掉 manual_disabled 的 entry"""
    cm = CooldownManager(cooldown_seconds=0)
    cm.mark_manual_disabled(1)
    cleaned = cm.cleanup()
    check("manual_disabled 不被 cleanup", 1 in cm.snapshot(), True)


def test_cleanup_removes_expired_normal():
    """cleanup 清掉 cooldown 已过期 + next_probe_at 已过期的 entry"""
    cm = CooldownManager(cooldown_seconds=0, max_probe_backoff_seconds=10)
    # 用 5xx + 短 probe_after=5s (但被 max 限制到10s?实际 max=10, probe_after=300, cap 不会生效)
    # 直接构造状态: cooldown 在过去, next_probe_at 在过去
    cm._state[1] = {
        "count": 1, "cooldown_until": time.time() - 100,  # 100s 前已过期
        "first_failure_at": time.time() - 200, "last_failure_at": time.time() - 100,
        "last_error_type": "upstream_5xx", "last_error_code": None, "last_error_msg": "",
        "needs_human": False, "next_probe_at": time.time() - 50,  # 50s 前已过期
        "probe_attempts": 1, "last_success_at": None, "manual_disabled_at": None,
    }
    cleaned = cm.cleanup()
    check("cleaned=1", cleaned, 1)
    check("state 已清", 1 in cm.snapshot(), False)


def main():
    tests = [
        test_healthy_to_failing,
        test_mark_success_resets_to_healthy,
        test_failing_to_degraded,
        test_quota_2_failures_needs_human,
        test_auth_1_failure_needs_human,
        test_5xx_never_needs_human,
        test_manual_disable_excludes_from_probe,
        test_manual_disable_then_enable,
        test_accounts_due_for_probe,
        test_needs_human_not_due_for_probe,
        test_accounts_needing_human,
        test_probe_result_success_clears_state,
        test_probe_result_failure_increments_probe_attempts,
        test_probe_result_failure_triggers_needs_human,
        test_exponential_backoff,
        test_backoff_capped,
        test_backward_compat_reason_string,
        test_backward_compat_reset,
        test_snapshot_full_fields,
        test_cleanup_keeps_needs_human,
        test_cleanup_keeps_manual_disabled,
        test_cleanup_removes_expired_normal,
    ]
    for t in tests:
        print(f"\n--- {t.__name__} ---")
        try:
            t()
        except Exception as e:
            global _failed
            _failed += 1
            print(f"  EXCEPTION: {e}")

    print(f"\n=== 结果: {_passed} passed, {_failed} failed ===")
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())