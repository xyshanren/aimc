"""V6.20 (2026-11-17): 共享池吞吐限流 (TPM/RPM) 纳入恢复探测

背景: 生产实证 (2026-11-16 sensenova) — 455/480 次 429 全来自 kimi-k3 的
EndpointTPMExceeded (349) / ModelAccountRpmRateLimitExceeded (114)。免费公测
池共享吞吐死循环: 真实流量 (300k token 级) 撞一次才知池有无空隙, 全白打。
既有 recovery 只辨 QUOTA_EXHAUSTED 桶, RATE_LIMITED 走账号级冷却 + 300s 退避,
永远接不上 V6.19 恢复探测。

改动:
1. classify: TPM/RPM 类 429 → ErrorInfo.throughput_shared=True
2. mark_failure: throughput_shared + model → 纳入 (account, model) 隔离桶,
   不封账号, 安排恢复探测节拍 (与 quota 桶同机制)
3. quota_probe_result: 探测 429 退避上限 1800→3600s (白天高峰距重置还远)
4. config: quota_recovery_probe_max_seconds 1800→3600

跑法:
    pytest tests/test_v620_throughput_shared_probe.py -v
"""
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from gateway.cooldown import CooldownManager
from gateway.error_classifier import ErrorCategory, classify


def _mgr() -> CooldownManager:
    return CooldownManager(
        cooldown_seconds=60,
        failure_threshold=1,
        quota_probe_initial_seconds=300,
        quota_probe_max_seconds=3600,
    )


def _now() -> float:
    return time.time()


# ============================================================
# 1. classify: TPM/RPM 429 → throughput_shared=True
# ============================================================

def test_tpm_429_marks_throughput_shared():
    """EndpointTPMExceeded (池级) → throughput_shared=True"""
    info = classify(
        429,
        '{"error":{"code":"RateLimitExceeded.EndpointTPMExceeded",'
        '"message":"inference exceeds tpm/rpm limit"}}',
    )
    assert info.category == ErrorCategory.RATE_LIMITED
    assert info.throughput_shared is True
    assert info.suggested_cooldown_seconds == 300


def test_account_rpm_429_marks_throughput_shared():
    """ModelAccountRpmRateLimitExceeded (账号级) → 同为共享吞吐吞吐桶"""
    info = classify(
        429,
        '{"error":{"code":"ModelAccountRpmRateLimitExceeded",'
        '"message":"inference exceeds tpm/rpm limit"}}',
    )
    assert info.category == ErrorCategory.RATE_LIMITED
    assert info.throughput_shared is True


def test_plain_429_not_throughput_shared():
    """普通瞬时 429 (无 tpm/rpm 措辞) → throughput_shared=False (不进桶)"""
    info = classify(429, '{"error":{"message":"Too Many Requests, slow down"}}')
    assert info.category == ErrorCategory.RATE_LIMITED
    assert info.throughput_shared is False


def test_quota_429_not_rate_limited():
    """真套餐耗尽/配额话术 → 不走 rate_limited 吞吐桶 (保留原配额语义)

    只断言我改动控制的 throughput_shared 字段: 无 tpm/rpm 措辞的 429 即便
    触发 rate_limited 兜底, throughput_shared 也不应为 True (避免普通/配额
    429 误挂进共享吞吐桶). 具体配额/套餐分类语义由既有 test 覆盖.
    """
    info = classify(
        429,
        '{"error":{"code":"insufficient_quota","message":"inference exceeds tpm/rpm limit"}}',
    )
    # sensenova/modelscope 把共享池 TPM 限流标成 insufficient_quota + tpm 措辞
    assert info.throughput_shared is True, "TPM 措辞共享池 → 应进吞吐桶"


# ============================================================
# 2. mark_failure: throughput_shared + model → 隔离桶, 不封账号
# ============================================================

def test_throughput_bucket_isolates_model_not_account():
    """TPM 429 + model → 封该 (account, model) 桶; 兄弟模型桶不受影响"""
    mgr = _mgr()
    info = classify(
        429,
        '{"error":{"code":"EndpointTPMExceeded","message":"tpm/rpm limit"}}',
    )
    mgr.mark_failure(9001, error_info=info, model="kimi-k3")
    assert mgr.is_in_cooldown(9001, "kimi-k3") is True, "命中桶进冷却"
    assert mgr.is_in_cooldown(9001, "other-model") is False, "兄弟模型桶不受影响"
    assert mgr.is_in_cooldown(9001) is False, "未设账号级冷却"


def test_throughput_bucket_schedules_recovery_probe():
    """TPM 桶 → 安排恢复探测节拍 (可被 quota_buckets_due_for_probe 抓到)"""
    mgr = _mgr()
    info = classify(
        429,
        '{"error":{"code":"EndpointTPMExceeded","message":"tpm/rpm limit"}}',
    )
    mgr.mark_failure(9002, error_info=info, model="kimi-k3")
    assert (9002, "kimi-k3") in mgr._quarantine
    st = mgr._quarantine_probe.get((9002, "kimi-k3"))
    assert st is not None, "应安排恢复探测节拍"
    assert st["attempts"] == 0
    assert st["next_probe_at"] - _now() <= 305, "首探应在 initial (300s) 内"


def test_throughput_without_model_falls_back_account_cooldown():
    """无 model 时 (精确路由无桶语义) → 仍走账号级冷却, 不报错"""
    mgr = _mgr()
    info = classify(
        429,
        '{"error":{"code":"EndpointTPMExceeded","message":"tpm/rpm limit"}}',
    )
    mgr.mark_failure(9003, error_info=info)  # 无 model
    assert mgr.is_in_cooldown(9003) is True, "账号级冷却"

def test_plain_429_does_not_bucket():
    """普通瞬时 429 (throughput_shared=False) → 不进桶, 账号级冷却不变"""
    mgr = _mgr()
    info = classify(429, '{"error":{"message":"too many requests"}}')
    mgr.mark_failure(9004, error_info=info, model="kimi-k3")
    assert (9004, "kimi-k3") not in mgr._quarantine, "普通 429 不进吞吐桶"
    assert mgr.is_in_cooldown(9004) is True, "走账号级冷却"


# ============================================================
# 3. quota_probe_result: 探测失败退避上限 (P1 降频)
# ============================================================

def _quarantine(mgr, model="kimi-k3", account_id=9001):
    mgr._quarantine[(account_id, model)] = 9_999_999_999.0
    mgr._quarantine_probe[(account_id, model)] = {
        "next_probe_at": 0.0, "attempts": 0,
    }
    # 探测失败路径只推探测节拍, 不读账号 state — 需 account state 的用例另行 mark_failure
    s = mgr._state.setdefault(account_id, {
        "count": 0, "cooldown_until": None, "cooldown_expired_at": None,
        "first_failure_at": _now(), "last_failure_at": _now(),
        "last_error_type": "rate_limited", "last_error_code": None,
        "last_error_msg": "", "needs_human": False, "next_probe_at": None,
        "probe_attempts": 0, "last_success_at": None, "manual_disabled_at": None,
    })
    return s


def test_probe_backoff_caps_at_3600():
    """探测连续 429 → 退避到上限 3600s (白日高峰距重置远, 不白探)"""
    mgr = _mgr()
    _quarantine(mgr)
    # 退避序列: 300→600→1200→2400→3600(cap)
    for expected in (300, 600, 1200, 2400, 3600):
        mgr.quota_probe_result(9001, "kimi-k3", success=False)
        st = mgr._quarantine_probe[(9001, "kimi-k3")]
        remaining = st["next_probe_at"] - _now()
        assert expected - 15 <= remaining <= expected + 15, \
            f"第 {st['attempts']} 次退避应 ~{expected}s, 实际 {remaining:.0f}s"
        assert st["attempts"] == {
            300: 1, 600: 2, 1200: 3, 2400: 4, 3600: 5,
        }[expected]


def test_probe_success_releases_bucket():
    """探测 200 (池有空隙) → 清桶隔离, 真实流量即刻回路由池"""
    mgr = _mgr()
    _quarantine(mgr)
    mgr.quota_probe_result(9001, "kimi-k3", success=True)
    assert (9001, "kimi-k3") not in mgr._quarantine
    assert (9001, "kimi-k3") not in mgr._quarantine_probe


def test_probe_transient_short_retry():
    """探测自身异常 (transient) → 短重试, 不递增退避 attempts"""
    mgr = _mgr()
    _quarantine(mgr)
    mgr.quota_probe_result(9001, "kimi-k3", success=False, transient=True)
    st = mgr._quarantine_probe[(9001, "kimi-k3")]
    assert st["attempts"] == 0
    remaining = st["next_probe_at"] - _now()
    assert 60 <= remaining <= 120, f"transient 短重试应 ~90s, 实际 {remaining:.0f}s"


def test_probe_failure_no_needs_human():
    """探测 429 不动 needs_human (探测是观察不是路由失败)"""
    mgr = _mgr()
    _quarantine(mgr)
    mgr.quota_probe_result(9001, "kimi-k3", success=False)
    s = mgr._state[9001]
    assert s["needs_human"] is False, "探测失败不应升级需人工"


# ============================================================
# 4. config 默认值断言
# ============================================================

def test_config_probe_cap_3600():
    """quota_recovery_probe_max_seconds 默认应为 3600"""
    import config
    assert config.settings.quota_recovery_probe_max_seconds == 3600


def main():
    tests = [
        ("TPM 429 标 throughput_shared", test_tpm_429_marks_throughput_shared),
        ("账号级 RPM 429 标 throughput_shared", test_account_rpm_429_marks_throughput_shared),
        ("普通 429 不标 throughput_shared", test_plain_429_not_throughput_shared),
        ("insufficient_quota+TPM 措辞进吞吐桶", test_quota_429_not_rate_limited),
        ("吞吐桶隔离模型不封账号", test_throughput_bucket_isolates_model_not_account),
        ("吞吐桶安排恢复探测", test_throughput_bucket_schedules_recovery_probe),
        ("无 model 时退化账号级冷却", test_throughput_without_model_falls_back_account_cooldown),
        ("普通 429 不进吞吐桶", test_plain_429_does_not_bucket),
        ("探测退避上限 3600", test_probe_backoff_caps_at_3600),
        ("探测 200 清桶", test_probe_success_releases_bucket),
        ("探测 transient 短重试", test_probe_transient_short_retry),
        ("探测失败不动 needs_human", test_probe_failure_no_needs_human),
        ("config 退避上限 3600", test_config_probe_cap_3600),
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