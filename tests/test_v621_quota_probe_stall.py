"""V6.21 (2026-11-23): 共享吞吐桶长满停探 — 钉桶等人工

背景: 生产实证 (2026-11-22 sensenova) — kimi-k3 免费公测池共享吞吐长期满载,
quota_recovery_probe 11-22 单日对 sensenova 组账号各探 71–73 次、11-23 各
39–40 次, 几乎全失败 (5% 成功率 / 610 请求 580 个 429)。指数退避封顶 1h 后
照样到达期→路由重撞→再隔离→再探测的死循环, 白打上游 70+ 次/天。

改动:
1. config: quota_probe_stall_after_attempts (默认 12) — 共享吞吐桶连续探测
   失败达到阈值即停探
2. cooldown.quota_probe_result: 非 transient 失败计数; 达阈值 → 把隔离桶钉到
   远期 (路由不再放行, 探测停止), 等人工 ack-recover / 份额排查
3. mark_success(force) / cleanup 同步清 _quarantine_stall

跑法:
    pytest tests/test_v621_quota_probe_stall.py -v
"""
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from gateway.cooldown import CooldownManager


def _now() -> float:
    return time.time()


def _mgr(stall: int = 12) -> CooldownManager:
    return CooldownManager(
        cooldown_seconds=60,
        failure_threshold=1,
        quota_probe_initial_seconds=300,
        quota_probe_max_seconds=3600,
        quota_probe_stall_after_attempts=stall,
    )


def _quarantine(mgr, model="kimi-k3", account_id=9001):
    mgr._quarantine[(account_id, model)] = 9_999_999_999.0
    mgr._quarantine_probe[(account_id, model)] = {
        "next_probe_at": 0.0, "attempts": 0,
    }
    mgr._state.setdefault(account_id, {
        "count": 0, "cooldown_until": None, "cooldown_expired_at": None,
        "first_failure_at": _now(), "last_failure_at": _now(),
        "last_error_type": "rate_limited", "last_error_code": None,
        "last_error_msg": "", "needs_human": False, "next_probe_at": None,
        "probe_attempts": 0, "last_success_at": None, "manual_disabled_at": None,
    })


def test_below_threshold_keeps_probing():
    """未达阈值: 探测失败只推退避, 桶照常继续被探"""
    mgr = _mgr(stall=5)
    _quarantine(mgr)
    original_until = mgr._quarantine[(9001, "kimi-k3")]
    mgr.quota_probe_result(9001, "kimi-k3", success=False)
    mgr.quota_probe_result(9001, "kimi-k3", success=False)
    st = mgr._quarantine_probe[(9001, "kimi-k3")]
    assert st["attempts"] == 2
    # 未达阈值: 桶 until 应保持原值 (没被钉到 365d 远期)
    assert mgr._quarantine[(9001, "kimi-k3")] == original_until, \
        "未达阈值不应钉桶 (until 应保持不变)"
    assert (9001, "kimi-k3") in mgr._quarantine_probe, "探测节拍应仍在 (继续探)"


def test_reaching_threshold_pins_bucket():
    """达阈值: 钉住隔离桶到远期 + 停探 (探测节拍移除)"""
    mgr = _mgr(stall=3)
    _quarantine(mgr)
    for _ in range(3):
        mgr.quota_probe_result(9001, "kimi-k3", success=False)
    # 桶被钉到远期 (365 天)
    until = mgr._quarantine[(9001, "kimi-k3")]
    assert until - _now() > 3600 * 24 * 300, f"应钉住 ~365d, 实际剩余 {until-_now():.0f}s"
    # 探测节拍移除 → 不再被 quota_buckets_due_for_probe 抓到
    assert (9001, "kimi-k3") not in mgr._quarantine_probe
    assert (9001, "kimi-k3") not in mgr.quota_buckets_due_for_probe()


def test_stall_count_only_on_definitive_failure():
    """只有"真没恢复" (上游明确拒绝) 才计数; transient 自身上异常不耗阈值"""
    mgr = _mgr(stall=2)
    _quarantine(mgr)
    # 1 次 transient + 1 次明确 429 → transient 不计数, 明确只到 1, 未达 2
    mgr.quota_probe_result(9001, "kimi-k3", success=False, transient=True)
    mgr.quota_probe_result(9001, "kimi-k3", success=False)
    assert (9001, "kimi-k3") in mgr._quarantine_probe, "未达阈值应继续探"
    # 再 1 次明确 429 → 达 2 → 停探钉桶
    mgr.quota_probe_result(9001, "kimi-k3", success=False)
    assert (9001, "kimi-k3") not in mgr._quarantine_probe, "达阈值停探"


def test_success_before_threshold_resets_stall():
    """未达阈值时的成功 → 清桶隔离 + 清 stall (回到路由池, 计数清零)"""
    mgr = _mgr(stall=5)
    _quarantine(mgr)
    mgr.quota_probe_result(9001, "kimi-k3", success=False)
    mgr.quota_probe_result(9001, "kimi-k3", success=True)
    assert (9001, "kimi-k3") not in mgr._quarantine
    assert (9001, "kimi-k3") not in mgr._quarantine_probe
    assert (9001, "kimi-k3") not in mgr._quarantine_stall


def test_manual_force_clears_pinned_bucket():
    """人工 ack-recover (force) 应清掉被钉桶 + stall, 放行路由"""
    mgr = _mgr(stall=2)
    _quarantine(mgr)
    for _ in range(2):
        mgr.quota_probe_result(9001, "kimi-k3", success=False)
    assert (9001, "kimi-k3") not in mgr._quarantine_probe, "已停探钉桶"
    assert (9001, "kimi-k3") in mgr._quarantine
    # 人工确认恢复
    mgr.mark_success(9001, model="kimi-k3", force=True)
    assert (9001, "kimi-k3") not in mgr._quarantine, "force 清掉被钉桶"
    assert (9001, "kimi-k3") not in mgr._quarantine_stall
    assert mgr.is_in_cooldown(9001, "kimi-k3") is False


def test_none_disables_stall_old_behavior():
    """stall=None (旧调用方未配置) → 无限探测, 行为不变"""
    mgr = _mgr(stall=None)
    _quarantine(mgr)
    for _ in range(3):
        mgr.quota_probe_result(9001, "kimi-k3", success=False)
    assert (9001, "kimi-k3") in mgr._quarantine_probe, "未配置阈值 = 继续探"


def test_config_default_is_12():
    """quota_probe_stall_after_attempts 默认应为 12"""
    import config
    assert config.settings.quota_probe_stall_after_attempts == 12


def main():
    tests = [
        ("未达阈值继续探", test_below_threshold_keeps_probing),
        ("达阈值钉桶停探", test_reaching_threshold_pins_bucket),
        ("仅明确拒绝计数", test_stall_count_only_on_definitive_failure),
        ("成功清桶+清 stall", test_success_before_threshold_resets_stall),
        ("人工 force 清被钉桶", test_manual_force_clears_pinned_bucket),
        ("None 维持旧行为", test_none_disables_stall_old_behavior),
        ("config 默认 12", test_config_default_is_12),
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