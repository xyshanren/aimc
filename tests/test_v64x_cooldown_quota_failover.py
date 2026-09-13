"""V6.4.x: 冷却指数退避 + 迟到成功守卫 + 渠道级限流同步 + 配额预检 failover

背景 (2026-09-13 生产诊断):
- sensenova 双账号共享 TPM 池 (429001), 固定 30s 账号级冷却导致解冻即重撞乒乓
- 长流式请求的"迟到成功"在冷却期内完成, mark_success 把冷却擦掉, 冷却永不生效
- routes.py 配额预检失败直接拒死请求 (不 failover / 不落库), deepseek_01 的
  tpm=100000 静态估算恒超 → deepseek-flash 兜底永久封死且看板不可见

覆盖:
- mark_failure rate_limited 指数退避 (30→60→120→240, 封顶 max_probe_backoff)
- 非 rate_limited 类别不退避 (5xx 固定 60s)
- mark_success 冷却期内 (迟到成功) 不复位 count / 不清冷却
- mark_success 冷却外 (真实健康) 全量复位
- 渠道级限流同步: 兄弟在 rate_limited 冷却中 → 冷却对齐兄弟剩余
- 同步只认 rate_limited 兄弟 (auth 冷却的兄弟不触发对齐)
- routes.py 配额预检: mark_failure + failed_channels 记录 + failover 决策模拟

跑法:
    python tests/test_v64x_cooldown_quota_failover.py
    或 pytest tests/test_v64x_cooldown_quota_failover.py -v
"""
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from gateway.cooldown import CooldownManager
from gateway.error_classifier import ErrorCategory, ErrorInfo


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


def _rate_limited_info(base=30):
    return ErrorInfo(
        category=ErrorCategory.RATE_LIMITED,
        error_code="429001",
        error_msg="inference exceeds tpm/rpm limit",
        needs_human=False,
        suggested_cooldown_seconds=base,
        suggested_probe_after_seconds=60,
    )


def _5xx_info():
    return ErrorInfo(
        category=ErrorCategory.UPSTREAM_5XX,
        error_code=None,
        error_msg="bad gateway",
        needs_human=False,
        suggested_cooldown_seconds=60,
        suggested_probe_after_seconds=300,
    )


def _auth_info():
    return ErrorInfo(
        category=ErrorCategory.AUTH_FAILED,
        error_code="invalid_api_key",
        error_msg="invalid key",
        needs_human=True,
        suggested_cooldown_seconds=86400,
        suggested_probe_after_seconds=None,
    )


# ============================================================
# rate_limited 指数退避
# ============================================================

def test_rate_limited_exponential_backoff():
    print("\n--- test_rate_limited_exponential_backoff ---")
    cm = CooldownManager(failure_threshold=1)
    aid = 1

    cm.mark_failure(aid, error_info=_rate_limited_info())
    cd1 = cm._state[aid]["cooldown_until"] - time.time()
    check("首败冷却 ~30s", 29 <= cd1 <= 31, True)

    # 模拟冷却瞬间过期, 连续第二次失败 (无成功间隔)
    cm._state[aid]["cooldown_until"] = time.time() - 1
    cm.mark_failure(aid, error_info=_rate_limited_info())
    cd2 = cm._state[aid]["cooldown_until"] - time.time()
    check("连败冷却 ~60s", 59 <= cd2 <= 61, True)

    cm._state[aid]["cooldown_until"] = time.time() - 1
    cm.mark_failure(aid, error_info=_rate_limited_info())
    cd3 = cm._state[aid]["cooldown_until"] - time.time()
    check("三连败冷却 ~120s", 119 <= cd3 <= 121, True)

    cm._state[aid]["cooldown_until"] = time.time() - 1
    cm.mark_failure(aid, error_info=_rate_limited_info())
    cd4 = cm._state[aid]["cooldown_until"] - time.time()
    check("四连败冷却 ~240s", 239 <= cd4 <= 241, True)


def test_rate_limited_backoff_capped():
    print("\n--- test_rate_limited_backoff_capped ---")
    cm = CooldownManager(failure_threshold=1, max_probe_backoff_seconds=120)
    aid = 2
    cd = None
    for _ in range(8):
        cm._state[aid] = {
            "count": cm._state.get(aid, {}).get("count", 0),
            "cooldown_until": None,
            "first_failure_at": time.time(),
            "last_failure_at": time.time(),
            "last_error_type": "", "last_error_code": None, "last_error_msg": "",
            "needs_human": False, "next_probe_at": None, "probe_attempts": 0,
            "last_success_at": None, "manual_disabled_at": None,
        }
        cm.mark_failure(aid, error_info=_rate_limited_info())
        cd = cm._state[aid]["cooldown_until"] - time.time()
        cm._state[aid]["cooldown_until"] = time.time() - 1
    check("8 连败后仍封顶 120s", 119 <= cd <= 121, True)
    check("rate_limited 永不 needs_human", cm._state[aid]["needs_human"], False)


def test_non_rate_limited_no_backoff():
    print("\n--- test_non_rate_limited_no_backoff ---")
    cm = CooldownManager(failure_threshold=1)
    aid = 3
    cm.mark_failure(aid, error_info=_5xx_info())
    cd1 = cm._state[aid]["cooldown_until"] - time.time()
    cm._state[aid]["cooldown_until"] = time.time() - 1
    cm.mark_failure(aid, error_info=_5xx_info())
    cd2 = cm._state[aid]["cooldown_until"] - time.time()
    check("5xx 首败 ~60s", 59 <= cd1 <= 61, True)
    check("5xx 再败仍 ~60s (不退避)", 59 <= cd2 <= 61, True)


# ============================================================
# mark_success 迟到成功守卫
# ============================================================

def test_stale_success_keeps_cooldown():
    print("\n--- test_stale_success_keeps_cooldown ---")
    cm = CooldownManager(failure_threshold=1)
    aid = 10
    cm.mark_failure(aid, error_info=_rate_limited_info())
    until = cm._state[aid]["cooldown_until"]
    count = cm._state[aid]["count"]

    # 冷却期内的"迟到成功" (在途长流式请求完成)
    cm.mark_success(aid)
    check("冷却未到期不清除", cm._state[aid]["cooldown_until"], until)
    check("count 保留 (退避延续)", cm._state[aid]["count"], count)
    check("仍在冷却中", cm.is_in_cooldown(aid), True)
    check("last_success_at 已记录", cm._state[aid]["last_success_at"] is not None, True)


def test_success_after_cooldown_expiry_resets():
    print("\n--- test_success_after_cooldown_expiry_resets ---")
    cm = CooldownManager(failure_threshold=1)
    aid = 11
    cm.mark_failure(aid, error_info=_rate_limited_info())
    # 模拟冷却已过期
    cm._state[aid]["cooldown_until"] = time.time() - 1
    cm.mark_success(aid)
    check("冷却外成功全量复位 count", cm._state[aid]["count"], 0)
    check("cooldown_until 清空", cm._state[aid]["cooldown_until"], None)
    check("is_in_cooldown False", cm.is_in_cooldown(aid), False)


def test_stale_success_then_failure_escalates():
    print("\n--- test_stale_success_then_failure_escalates ---")
    # 生产场景还原: 429 冷却 → 迟到 200 (不清) → 冷却过期再撞 → 退避到 60s
    cm = CooldownManager(failure_threshold=1)
    aid = 12
    cm.mark_failure(aid, error_info=_rate_limited_info())   # 30s
    cm.mark_success(aid)                                    # 迟到成功, 不清
    cm._state[aid]["cooldown_until"] = time.time() - 1      # 冷却过期
    cm.mark_failure(aid, error_info=_rate_limited_info())   # count=2 → 60s
    cd = cm._state[aid]["cooldown_until"] - time.time()
    check("再败退避到 ~60s (未被迟到成功清零)", 59 <= cd <= 61, True)


# ============================================================
# 渠道级限流同步 (兄弟账号)
# ============================================================

def test_sibling_rate_limited_sync():
    print("\n--- test_sibling_rate_limited_sync ---")
    cm = CooldownManager(failure_threshold=1)
    a1, a2 = 20, 21
    # 兄弟 a2 两连败 → 60s 冷却
    cm.mark_failure(a2, error_info=_rate_limited_info())
    cm._state[a2]["cooldown_until"] = time.time() - 1
    cm.mark_failure(a2, error_info=_rate_limited_info())
    a2_until = cm._state[a2]["cooldown_until"]
    check("a2 冷却 ~60s", 59 <= a2_until - time.time() <= 61, True)

    # a1 首败 (基准 30s), 但兄弟 a2 在 rate_limited 冷却中 → 对齐 a2 剩余
    cm.mark_failure(a1, error_info=_rate_limited_info(), sibling_account_ids=[a2])
    check("a1 冷却对齐兄弟 (~60s)",
          abs(cm._state[a1]["cooldown_until"] - a2_until) < 1.0, True)


def test_sibling_sync_ignores_auth_cooldown():
    print("\n--- test_sibling_sync_ignores_auth_cooldown ---")
    cm = CooldownManager(failure_threshold=1)
    a4, a5 = 30, 31
    cm.mark_failure(a4, error_info=_auth_info())  # auth 86400s
    cm.mark_failure(a5, error_info=_rate_limited_info(), sibling_account_ids=[a4])
    cd = cm._state[a5]["cooldown_until"] - time.time()
    check("auth 兄弟不触发对齐, a5 仍 ~30s", 29 <= cd <= 31, True)


def test_no_sibling_no_sync():
    print("\n--- test_no_sibling_no_sync ---")
    cm = CooldownManager(failure_threshold=1)
    aid = 40
    cm.mark_failure(aid, error_info=_rate_limited_info(), sibling_account_ids=[])
    cd = cm._state[aid]["cooldown_until"] - time.time()
    check("无兄弟时基准 ~30s", 29 <= cd <= 31, True)


# ============================================================
# routes.py 配额预检 failover 语义 (mark_failure + 决策模拟)
# ============================================================

def _quota_info():
    return ErrorInfo(
        category=ErrorCategory.QUOTA_EXHAUSTED,
        error_code=None,
        error_msg="TPM 超额 (0+256000 > 100000)",
        needs_human=True,
        suggested_cooldown_seconds=3600,
        suggested_probe_after_seconds=None,
    )


def test_quota_reject_marks_account_for_skip():
    print("\n--- test_quota_reject_marks_account_for_skip ---")
    # 模拟 routes.py 预检拒绝后的 mark_failure: 路由层 (select_best_channel
    # 的 is_in_cooldown 过滤) 据此跳过该账号, 重选下一候选
    cm = CooldownManager(failure_threshold=1)
    deepseek_01 = 1
    cm.mark_failure(deepseek_01, error_info=_quota_info())
    check("预检拒绝后账号进冷却 (路由跳过)", cm.is_in_cooldown(deepseek_01), True)
    check("冷却 ~3600s", cm.cooldown_remaining(deepseek_01) > 3500, True)
    check("last_error_type=quota_exhausted",
          cm.snapshot()[deepseek_01]["last_error_type"], "quota_exhausted")


def test_quota_reject_failover_decision():
    print("\n--- test_quota_reject_failover_decision ---")
    # 模拟 routes.py 重试循环: 预检拒绝 → attempt < max_retries 则 continue 重选,
    # 重选因冷却过滤拿到下一候选; 用尽重试才返回 429
    cm = CooldownManager(failure_threshold=1)
    deepseek_01, sensenova_a, sensenova_b = 1, 5, 7
    cm.mark_failure(deepseek_01, error_info=_quota_info())

    candidates = [deepseek_01, sensenova_a, sensenova_b]
    max_retries = 2
    tried = []
    for attempt in range(max_retries + 1):
        available = [a for a in candidates if not cm.is_in_cooldown(a)]
        if not available:
            break  # select_channel 返回 None → 503 (全部不可用)
        picked = available[0]
        tried.append(picked)
        if attempt < max_retries:
            cm.mark_failure(picked, error_info=_quota_info())  # 预检再拒 → continue
            continue
    check("拒绝账号被跳过, 重选命中下一候选", tried, [sensenova_a, sensenova_b])


def test_legacy_mark_failure_unchanged():
    print("\n--- test_legacy_mark_failure_unchanged ---")
    # 向后兼容: 只传 reason 的旧调用保持固定 60s, 不退避不同步
    cm = CooldownManager(cooldown_seconds=60, failure_threshold=1)
    aid = 50
    cm.mark_failure(aid, reason="legacy")
    cd1 = cm._state[aid]["cooldown_until"] - time.time()
    cm._state[aid]["cooldown_until"] = time.time() - 1
    cm.mark_failure(aid, reason="legacy")
    cd2 = cm._state[aid]["cooldown_until"] - time.time()
    check("旧路径首败 ~60s", 59 <= cd1 <= 61, True)
    check("旧路径再败仍 ~60s", 59 <= cd2 <= 61, True)


# ============================================================
# runner
# ============================================================

if __name__ == "__main__":
    test_rate_limited_exponential_backoff()
    test_rate_limited_backoff_capped()
    test_non_rate_limited_no_backoff()
    test_stale_success_keeps_cooldown()
    test_success_after_cooldown_expiry_resets()
    test_stale_success_then_failure_escalates()
    test_sibling_rate_limited_sync()
    test_sibling_sync_ignores_auth_cooldown()
    test_no_sibling_no_sync()
    test_quota_reject_marks_account_for_skip()
    test_quota_reject_failover_decision()
    test_legacy_mark_failure_unchanged()
    print(f"\n{'='*50}\n通过 {_passed}, 失败 {_failed}")
    sys.exit(1 if _failed else 0)
