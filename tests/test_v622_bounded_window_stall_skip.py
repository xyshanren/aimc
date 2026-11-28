"""V6.22 bounded-window stall-skip tests (glm pin incident fix).

生产实证 2026-11-23→11-28: glm 周配额 11-21 打穿, 桶隔离到 11-26 16:01:15
(消息声明重置点)。11-23 V6.21 部署重启后探测节拍重建, 重置点之前的探测失败
计入停探阈值, 12 次探满 → glm-5.3 (11-24 04:44) / glm-5.3-flash (11-24 19:03)
被钉 365d。11-26 16:01 上游重置后既无探测 (停探) 也无流量 (钉桶挡路由) →
20h+ 不自愈。

修复: quota_probe_result 对"恢复点未到"的探测失败不计数 — 双信号判定
(bounded 桶 until 在未来 / 失败消息声明未来重置点); 共享吞吐桶照常计数
(V6.21 不回归); 恢复点已过仍失败照常计数 (真停满仍钉桶等人工)。
"""
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from gateway.cooldown import CooldownManager
from gateway.error_classifier import (
    ErrorCategory, ErrorInfo, parse_quota_reset_datetime,
)


def _mgr(stall=12):
    return CooldownManager(
        cooldown_seconds=60,
        failure_threshold=1,
        quota_probe_initial_seconds=300,
        quota_probe_max_seconds=3600,
        quota_probe_stall_after_attempts=stall,
    )


def _state_row():
    return {
        "count": 0, "cooldown_until": None, "cooldown_expired_at": None,
        "first_failure_at": time.time(), "last_failure_at": time.time(),
        "last_error_type": "rate_limited", "last_error_code": None,
        "last_error_msg": "", "needs_human": False, "next_probe_at": None,
        "probe_attempts": 0, "last_success_at": None,
        "manual_disabled_at": None,
    }


def _bounded_bucket(mgr, aid=9001, model="glm-5.3", until_offset=7200.0):
    """有界配额桶: 走真实 mark_failure 路径 (quota_exhausted, 无吞吐标记)"""
    info = ErrorInfo(
        category=ErrorCategory.QUOTA_EXHAUSTED,
        error_code="1308",
        error_msg="已达到 5 小时的使用上限。",
        needs_human=True,
        suggested_cooldown_seconds=int(until_offset),
        suggested_probe_after_seconds=None,
    )
    mgr.mark_failure(aid, error_info=info, model=model)


def _throughput_bucket(mgr, aid=9001, model="kimi-k3"):
    """共享吞吐桶: V6.20/6.21 场景 (无重置点)"""
    info = ErrorInfo(
        category=ErrorCategory.RATE_LIMITED,
        error_code="429001",
        error_msg="EndpointTPMExceeded",
        needs_human=False,
        suggested_cooldown_seconds=3600,
        suggested_probe_after_seconds=None,
        throughput_shared=True,
    )
    mgr.mark_failure(aid, error_info=info, model=model)


# ===== 1. 有界桶恢复点未到 → 不计数不钉 =====

def test_bounded_until_future_never_pins():
    """核心场景: bounded 桶 until 在未来 + 消息无重置时间 → 15 次失败不钉"""
    mgr = _mgr(stall=12)
    _bounded_bucket(mgr, until_offset=4 * 86400)  # 周配额窗口 ~4.2d
    for _ in range(15):
        mgr.quota_probe_result(9001, "glm-5.3", success=False,
                               error_msg="已达到使用上限。")
    st = mgr._quarantine_probe[(9001, "glm-5.3")]
    assert st["attempts"] == 15, "探测节拍应照常推进"
    assert mgr._quarantine_stall.get((9001, "glm-5.3"), 0) == 0, \
        "恢复点未到不计数"
    assert (9001, "glm-5.3") in mgr._quarantine_probe, "继续探"
    assert mgr._quarantine[(9001, "glm-5.3")] - time.time() > 3 * 86400, \
        "桶 until 保持原值 (未被钉到 365d)"


def test_bounded_until_past_counts_and_pins():
    """恢复点已过仍失败 → 计数 → 达阈值钉桶 (上游迟重置/真停满)"""
    mgr = _mgr(stall=3)
    _bounded_bucket(mgr, until_offset=3600)
    mgr._quarantine[(9001, "glm-5.3")] = time.time() - 10  # 恢复点拨到过去
    for _ in range(3):
        mgr.quota_probe_result(9001, "glm-5.3", success=False,
                               error_msg="已达到使用上限。")
    until = mgr._quarantine[(9001, "glm-5.3")]
    assert until - time.time() > 3600 * 24 * 300, "恢复点已过仍失败 → 钉桶"
    assert (9001, "glm-5.3") not in mgr._quarantine_probe, "停探"


# ===== 2. 消息重置点信号 (clause 2) =====

def test_message_future_reset_skips_counting():
    """无 bounded 标记的桶 + 消息声明未来重置点 → 不计不钉
    (兜底 until 被 _RESET_MAX_SECONDS 7d 解析上限截短的场景)"""
    mgr = _mgr(stall=3)
    # V6.21 测试同款手工造桶 (无 bounded 标记, until 远期)
    mgr._quarantine[(9001, "kimi-k3")] = 9_999_999_999.0
    mgr._quarantine_probe[(9001, "kimi-k3")] = {"next_probe_at": 0.0, "attempts": 0}
    mgr._state.setdefault(9001, _state_row())
    future = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
    msg = f"已达到每周使用上限。您的限额将在 {future} 重置。"
    for _ in range(5):
        mgr.quota_probe_result(9001, "kimi-k3", success=False, error_msg=msg)
    assert (9001, "kimi-k3") in mgr._quarantine_probe, "恢复点未到不计数不钉"
    assert mgr._quarantine_stall.get((9001, "kimi-k3"), 0) == 0


def test_message_past_reset_counts():
    """消息声明的重置点已过仍失败 → 计数 → 钉桶 (真停满)"""
    mgr = _mgr(stall=2)
    mgr._quarantine[(9001, "kimi-k3")] = 9_999_999_999.0
    mgr._quarantine_probe[(9001, "kimi-k3")] = {"next_probe_at": 0.0, "attempts": 0}
    mgr._state.setdefault(9001, _state_row())
    past = (datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    msg = f"已达到使用上限。您的限额将在 {past} 重置。"
    for _ in range(2):
        mgr.quota_probe_result(9001, "kimi-k3", success=False, error_msg=msg)
    assert (9001, "kimi-k3") not in mgr._quarantine_probe, "重置点已过仍失败 → 钉"
    assert mgr._quarantine_stall[(9001, "kimi-k3")] == 2


# ===== 3. V6.21 语义不回归 + 埋点 =====

def test_throughput_bucket_counts_as_v621():
    """共享吞吐桶: 探测失败照常计数 → 钉桶语义不变"""
    mgr = _mgr(stall=3)
    _throughput_bucket(mgr)
    for _ in range(3):
        mgr.quota_probe_result(9001, "kimi-k3", success=False)
    until = mgr._quarantine[(9001, "kimi-k3")]
    assert until - time.time() > 3600 * 24 * 300, "吞吐桶照常钉桶"
    assert (9001, "kimi-k3") not in mgr._quarantine_probe


def test_mark_failure_buckets_carry_bounded_flag():
    """mark_failure 埋点: quota_exhausted → bounded=True, 吞吐桶 → falsy"""
    mgr = _mgr(stall=12)
    _bounded_bucket(mgr, model="glm-5.3")
    _throughput_bucket(mgr, model="kimi-k3")
    assert mgr._quarantine_probe[(9001, "glm-5.3")].get("bounded") is True
    assert not mgr._quarantine_probe[(9001, "kimi-k3")].get("bounded")


# ===== 4. parse_quota_reset_datetime 单元 =====

def test_parse_quota_reset_datetime_units():
    """有重置语义+时间 → dt; 缺一 → None"""
    msg = "已达到 5 小时的使用上限。您的限额将在 2026-11-27 16:54:52 重置。"
    dt = parse_quota_reset_datetime(msg)
    assert dt is not None
    assert (dt.year, dt.month, dt.day, dt.hour, dt.minute) == (2026, 11, 27, 16, 54)
    assert parse_quota_reset_datetime("余额不足，请充值") is None
    assert parse_quota_reset_datetime("请等待重置") is None
    assert parse_quota_reset_datetime("") is None
    assert parse_quota_reset_datetime("限额将在 2099-13-99 00:00:00 重置") is None


def main():
    tests = [
        ("有界桶恢复点未到不钉", test_bounded_until_future_never_pins),
        ("恢复点已过 → 钉", test_bounded_until_past_counts_and_pins),
        ("消息未来重置点不计", test_message_future_reset_skips_counting),
        ("消息过去重置点 → 计数", test_message_past_reset_counts),
        ("吞吐桶照常计数", test_throughput_bucket_counts_as_v621),
        ("mark_failure 埋点", test_mark_failure_buckets_carry_bounded_flag),
        ("parse 单元", test_parse_quota_reset_datetime_units),
    ]
    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  [OK] {name}")
            passed += 1
        except AssertionError as e:
            print(f"  [FAIL] {name}: {e}")
            failed += 1
        except Exception as e:
            print(f"  [ERROR] {name}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\nResult: {passed} passed, {failed} failed, {len(tests)} total")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
