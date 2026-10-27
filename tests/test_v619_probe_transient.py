"""V6.19 (2026-10-27): 配额恢复探测异常降级测试

背景: 2026-10-27 一周观察实证 — minimax 5h 窗口重置瞬间, 恢复探测 1-token
聊天超时 (自身异常), 被当"未恢复"吃满 30 分钟指数退避; 用户手点"确认恢复"
(GET /models, triggered_by=recovery_check) 立即放行。两类失败要区分:
- 上游明确拒绝 (429): 真没恢复, 维持 300s→1800s 指数退避
- 探测自身异常 (transient): 不确定状态 → 短重试 (quota_probe_transient_seconds,
  默认 90s), 不递增 attempts (异常轮不消耗退避曲线), 且 /models 轻探活佐证

跑法:
    pytest tests/test_v619_probe_transient.py -v
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from gateway.cooldown import CooldownManager


def _mgr() -> CooldownManager:
    return CooldownManager(
        cooldown_seconds=60,
        failure_threshold=1,
        quota_probe_initial_seconds=300,
        quota_probe_max_seconds=1800,
    )


def _quarantine(mgr, model="minimax-m3.1-flash", account_id=6):
    """注册一个配额桶隔离 (模拟 mark_failure quota 路径的结果状态)"""
    mgr._quarantine[(account_id, model)] = 9_999_999_999.0
    mgr._quarantine_probe[(account_id, model)] = {
        "next_probe_at": 0.0, "attempts": 0,
    }


# ============================================================
# 上游明确拒绝 (429) → 原指数退避, attempts 递增
# ============================================================

def test_upstream_reject_keeps_backoff():
    mgr = _mgr()
    _quarantine(mgr)
    mgr.quota_probe_result(6, "minimax-m3.1-flash", success=False)
    st = mgr._quarantine_probe[(6, "minimax-m3.1-flash")]
    assert st["attempts"] == 1
    remaining = st["next_probe_at"] - _now()
    assert 290 <= remaining <= 310, f"首退避应 ~300s, 实际 {remaining:.0f}s"


def test_upstream_reject_backoff_doubles():
    mgr = _mgr()
    _quarantine(mgr)
    mgr.quota_probe_result(6, "minimax-m3.1-flash", success=False)
    mgr.quota_probe_result(6, "minimax-m3.1-flash", success=False)
    st = mgr._quarantine_probe[(6, "minimax-m3.1-flash")]
    assert st["attempts"] == 2
    remaining = st["next_probe_at"] - _now()
    assert 590 <= remaining <= 620, f"二次退避应 ~600s, 实际 {remaining:.0f}s"


# ============================================================
# 探测自身异常 (transient) → 短重试, 不递增 attempts
# ============================================================

def test_transient_short_retry_no_attempts_growth():
    """异常轮: ~90s 短重试, attempts 不动 (不消耗退避曲线)"""
    mgr = _mgr()
    _quarantine(mgr)
    mgr.quota_probe_result(6, "minimax-m3.1-flash", success=False, transient=True)
    st = mgr._quarantine_probe[(6, "minimax-m3.1-flash")]
    assert st["attempts"] == 0, "异常轮不应递增退避计数"
    remaining = st["next_probe_at"] - _now()
    assert 60 <= remaining <= 120, f"异常短重试应 ~90s, 实际 {remaining:.0f}s"


def test_transient_does_not_extend_backoff_curve():
    """先异常再真 429: 退避曲线从 attempts=0 重新起算 (异常轮白嫖)"""
    mgr = _mgr()
    _quarantine(mgr)
    mgr.quota_probe_result(6, "minimax-m3.1-flash", success=False, transient=True)
    mgr.quota_probe_result(6, "minimax-m3.1-flash", success=False)
    st = mgr._quarantine_probe[(6, "minimax-m3.1-flash")]
    assert st["attempts"] == 1
    remaining = st["next_probe_at"] - _now()
    assert 290 <= remaining <= 310, "真拒绝应仍是首档 ~300s"


def test_transient_env_override():
    """AIMC_QUOTA_PROBE_TRANSIENT_SECONDS 可调 (config mock 验证 getattr 链)"""
    mgr = _mgr()
    _quarantine(mgr)
    import config
    orig = getattr(config.settings, "quota_probe_transient_seconds", None)
    try:
        config.settings.quota_probe_transient_seconds = 60
        mgr.quota_probe_result(6, "minimax-m3.1-flash", success=False, transient=True)
        st = mgr._quarantine_probe[(6, "minimax-m3.1-flash")]
        remaining = st["next_probe_at"] - _now()
        assert 45 <= remaining <= 80, f"env=60 时应 ~60s, 实际 {remaining:.0f}s"
    finally:
        if orig is None:
            if hasattr(config.settings, "quota_probe_transient_seconds"):
                delattr(config.settings, "quota_probe_transient_seconds")
        else:
            config.settings.quota_probe_transient_seconds = orig


# ============================================================
# 成功路径不变 (回归锚点)
# ============================================================

def test_success_clears_bucket():
    mgr = _mgr()
    _quarantine(mgr)
    mgr.quota_probe_result(6, "minimax-m3.1-flash", success=True)
    assert (6, "minimax-m3.1-flash") not in mgr._quarantine
    assert (6, "minimax-m3.1-flash") not in mgr._quarantine_probe


def test_success_after_transient_still_works():
    """异常轮后真恢复 → 正常清桶"""
    mgr = _mgr()
    _quarantine(mgr)
    mgr.quota_probe_result(6, "minimax-m3.1-flash", success=False, transient=True)
    mgr.quota_probe_result(6, "minimax-m3.1-flash", success=True)
    assert (6, "minimax-m3.1-flash") not in mgr._quarantine


def _now() -> float:
    import time
    return time.time()


# ============================================================
# 源码断言: channel_health 探测异常路径带 transient 标记
# ============================================================

def test_chat_probe_marks_transient():
    """_chat_probe_one 源码: 超时/连接异常分支 transient=True + /models 佐证"""
    import intelligence.channel_health as ch
    import inspect
    src = inspect.getsource(ch._chat_probe_one)
    assert '"transient": False' in src, "上游明确响应分支应标 transient=False"
    assert '"transient": True' in src, "异常分支应标 transient=True"
    assert "models_ok" in src, "异常分支应带 /models 佐证结果"


def test_probe_result_passes_transient():
    """quota_recovery_probe 主循环: 把 probe 的 transient 传给 cooldown_manager"""
    import intelligence.channel_health as ch
    import inspect
    src = inspect.getsource(ch.quota_recovery_probe)
    assert "transient=bool(probe.get(\"transient\"))" in src


def main():
    tests = [
        ("上游拒绝维持 ~300s 首档", test_upstream_reject_keeps_backoff),
        ("上游拒绝退避翻倍", test_upstream_reject_backoff_doubles),
        ("transient 短重试不递增 attempts", test_transient_short_retry_no_attempts_growth),
        ("transient 不延长退避曲线", test_transient_does_not_extend_backoff_curve),
        ("transient 间隔 env 可调", test_transient_env_override),
        ("成功清桶 (回归)", test_success_clears_bucket),
        ("异常后恢复仍清桶", test_success_after_transient_still_works),
        ("_chat_probe_one 异常分支标 transient", test_chat_probe_marks_transient),
        ("主循环透传 transient", test_probe_result_passes_transient),
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
