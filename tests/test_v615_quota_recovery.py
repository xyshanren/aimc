"""V6.15 (2026-09-28): 配额恢复感知 + 冷却/频限告警纠正 测试

覆盖:
1. error_classifier 解析上游重置时间 (GLM "限额将在 <时间> 重置" → 冷却到重置点)
2. CooldownManager 配额桶恢复探测节拍 (_quarantine_probe / quota_probe_result)
3. channel_health._chat_probe_one / quota_recovery_probe (1-token 真聊天感知恢复)
4. router 冷却/频限过滤告警 (原「频限已满」误报冷却 → 分原因报告)

跑法:
    python tests/test_v615_quota_recovery.py
"""
import asyncio
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from gateway.error_classifier import ErrorCategory, ErrorInfo, classify
from gateway.cooldown import CooldownManager


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


def _quota_info(msg: str, model: str = "glm-5.3") -> ErrorInfo:
    return ErrorInfo(
        category=ErrorCategory.QUOTA_EXHAUSTED,
        error_code="1308",
        error_msg=msg,
        needs_human=True,
        suggested_cooldown_seconds=3600,
        suggested_probe_after_seconds=1800,
    )


# ===== 1. classify 重置时间解析 =====

def test_glm_reset_message_shortens_cooldown():
    """生产实证消息: 冷却 = 重置点 - now + 30s 缓冲 (而非固定 3600)"""
    reset = datetime.now() + timedelta(hours=1)
    body = (
        '{"error":{"code":"1308","message":"已达到 5 小时的使用上限。'
        f'您的限额将在 {reset.strftime("%Y-%m-%d %H:%M:%S")} 重置。"}}'
    )
    info = classify(429, body, None)
    check("category", info.category, ErrorCategory.QUOTA_EXHAUSTED)
    ok = 3590 <= info.suggested_cooldown_seconds <= 3640
    check("cooldown ≈ 3600+buffer", ok, True)


def test_reset_far_future_respected():
    """重置点在 3 天后 → 冷却 3 天+buffer (而非 3600 反复重撞)"""
    reset = datetime.now() + timedelta(days=3)
    msg = f"已达到本周使用上限。您的限额将在 {reset.strftime('%Y-%m-%d %H:%M:%S')} 重置。"
    info = classify(429, '{"error":{"message":"%s"}}' % msg, None)
    ok = 3 * 86400 <= info.suggested_cooldown_seconds <= 3 * 86400 + 60
    check("3d reset respected", ok, True)


def test_no_reset_keyword_keeps_default():
    """无重置语义词 → 默认 3600 (行为不变)"""
    info = classify(429, '{"error":{"message":"余额不足，请充值"}}')
    check("no keyword default", info.suggested_cooldown_seconds, 3600)


def test_reset_keyword_without_time_keeps_default():
    """有重置语义但无绝对时间 → 默认 3600"""
    info = classify(429, '{"error":{"message":"余额不足，请等待重置"}}')
    check("category", info.category, ErrorCategory.QUOTA_EXHAUSTED)
    check("keyword no time default", info.suggested_cooldown_seconds, 3600)


def test_past_reset_time_min_cooldown():
    """重置点已过 (时钟偏差) → 最小 60s, 让路由自然复检"""
    reset = datetime.now() - timedelta(minutes=5)
    msg = f"已达到使用上限。您的限额将在 {reset.strftime('%Y-%m-%d %H:%M:%S')} 重置。"
    info = classify(429, '{"error":{"message":"%s"}}' % msg, None)
    check("past reset → 60s", info.suggested_cooldown_seconds, 60)


def test_invalid_datetime_keeps_default():
    """非法时间 (2026-13-99) → 默认 3600"""
    info = classify(429, '{"error":{"message":"已达到上限。将在 2026-13-99 25:61 重置。"}}')
    check("invalid datetime default", info.suggested_cooldown_seconds, 3600)


def test_english_reset_parses():
    """英文 reset 消息同样解析"""
    reset = datetime.now() + timedelta(hours=2)
    body = (
        '{"error":{"code":"insufficient_quota","message":'
        f'"plan quota exhausted, will reset at {reset.strftime("%Y-%m-%d %H:%M:%S")}"}}'
    )
    info = classify(429, body, None)
    check("category", info.category, ErrorCategory.QUOTA_EXHAUSTED)
    ok = 7190 <= info.suggested_cooldown_seconds <= 7250
    check("english reset parsed", ok, True)


# ===== 2. CooldownManager 配额桶恢复探测节拍 =====

def _mk_cm(**kw) -> CooldownManager:
    return CooldownManager(quota_probe_initial_seconds=300,
                           quota_probe_max_seconds=1800, **kw)


def test_quarantine_schedules_recovery_probe():
    cm = _mk_cm()
    cm.mark_failure(2, error_info=_quota_info("上限"), model="glm-5.3")
    check("bucket quarantined", (2, "glm-5.3") in cm._quarantine, True)
    st = cm._quarantine_probe.get((2, "glm-5.3"))
    check("probe scheduled", bool(st) and st["attempts"] == 0, True)
    ok = 290 <= st["next_probe_at"] - time.time() <= 310
    check("first probe ≈ 300s", ok, True)
    check("not due yet", cm.quota_buckets_due_for_probe(), [])


def test_probe_due_when_time_reached():
    cm = _mk_cm()
    cm.mark_failure(2, error_info=_quota_info("上限"), model="glm-5.3")
    cm._quarantine_probe[(2, "glm-5.3")]["next_probe_at"] = time.time() - 1
    check("due", cm.quota_buckets_due_for_probe(), [(2, "glm-5.3")])


def test_probe_success_clears_bucket():
    cm = _mk_cm()
    cm.mark_failure(2, error_info=_quota_info("上限"), model="glm-5.3")
    cm.quota_probe_result(2, "glm-5.3", success=True)
    check("bucket cleared", (2, "glm-5.3") in cm._quarantine, False)
    check("is_in_cooldown False",
          cm.is_in_cooldown(2, "glm-5.3"), False)
    check("probe entry cleared", (2, "glm-5.3") in cm._quarantine_probe, False)


def test_probe_failure_backs_off_without_touching_count():
    cm = _mk_cm()
    cm.mark_failure(2, error_info=_quota_info("上限"), model="glm-5.3")
    before_count = cm._state[2]["count"]
    cm.quota_probe_result(2, "glm-5.3", success=False)
    check("still quarantined", (2, "glm-5.3") in cm._quarantine, True)
    check("count untouched", cm._state[2]["count"], before_count)
    check("needs_human untouched", cm._state[2]["needs_human"], False)
    st = cm._quarantine_probe[(2, "glm-5.3")]
    check("attempts=1", st["attempts"], 1)
    ok = 290 <= st["next_probe_at"] - time.time() <= 310
    check("backoff #1 ≈ 300s", ok, True)
    cm.quota_probe_result(2, "glm-5.3", success=False)
    st = cm._quarantine_probe[(2, "glm-5.3")]
    ok = 590 <= st["next_probe_at"] - time.time() <= 620
    check("backoff #2 ≈ 600s", ok, True)


def test_probe_backoff_capped():
    cm = _mk_cm()
    cm.mark_failure(2, error_info=_quota_info("上限"), model="glm-5.3")
    for _ in range(6):
        cm.quota_probe_result(2, "glm-5.3", success=False)
    st = cm._quarantine_probe[(2, "glm-5.3")]
    ok = 1790 <= st["next_probe_at"] - time.time() <= 1810
    check("backoff capped at 1800s", ok, True)


def test_needs_human_account_not_probed():
    cm = _mk_cm()
    cm.mark_failure(2, error_info=_quota_info("上限"), model="glm-5.3")
    cm.mark_failure(2, error_info=_quota_info("上限"), model="glm-5.3")  # count=2 → needs_human
    check("needs_human raised", cm._state[2]["needs_human"], True)
    cm._quarantine_probe[(2, "glm-5.3")]["next_probe_at"] = time.time() - 1
    check("not probed", cm.quota_buckets_due_for_probe(), [])


def test_expired_bucket_not_probed():
    cm = _mk_cm()
    cm.mark_failure(2, error_info=_quota_info("上限"), model="glm-5.3")
    cm._quarantine[(2, "glm-5.3")] = time.time() - 1
    cm._quarantine_probe[(2, "glm-5.3")]["next_probe_at"] = time.time() - 1
    check("expired not probed", cm.quota_buckets_due_for_probe(), [])
    check("expired is_in_cooldown False",
          cm.is_in_cooldown(2, "glm-5.3"), False)


def test_cleanup_drops_orphan_probe_entries():
    cm = _mk_cm()
    cm.mark_failure(2, error_info=_quota_info("上限"), model="glm-5.3")
    cm._quarantine.pop((2, "glm-5.3"))  # 模拟桶被 mark_success 清掉但节拍残留
    cm.cleanup()
    check("orphan probe dropped", (2, "glm-5.3") in cm._quarantine_probe, False)


def test_cooldown_remaining_bucket_semantics():
    cm = _mk_cm()
    cm.mark_failure(2, error_info=_quota_info("上限"), model="glm-5.3")
    rem = cm.cooldown_remaining(2, "glm-5.3")
    check("bucket remaining > 0", rem > 0, True)
    check("account-level remaining 0 (只有桶隔离)", cm.cooldown_remaining(2), 0.0)


# ===== 3. _chat_probe_one / quota_recovery_probe =====

def _mk_response(status_code, text='{"choices":[]}'):
    r = MagicMock()
    r.status_code = status_code
    r.text = text
    return r


def test_chat_probe_one_200_is_recovery():
    from intelligence.channel_health import _chat_probe_one
    client = MagicMock()
    client.post = AsyncMock(return_value=_mk_response(200))
    with patch("intelligence.channel_health._get_probe_client", return_value=client):
        r = asyncio.run(_chat_probe_one(2, "https://x/api/coding/paas/v4", "sk-mock", "glm-5.3"))
    check("200 → success", r["success"], True)
    check("post url", client.post.call_args.args[0],
          "https://x/api/coding/paas/v4/chat/completions")
    check("max_tokens=1", client.post.call_args.kwargs["json"]["max_tokens"], 1)


def test_chat_probe_one_429_not_recovered():
    from intelligence.channel_health import _chat_probe_one
    client = MagicMock()
    client.post = AsyncMock(return_value=_mk_response(
        429, '{"error":{"code":1308,"message":"已达到 5 小时的使用上限"}}'))
    with patch("intelligence.channel_health._get_probe_client", return_value=client):
        r = asyncio.run(_chat_probe_one(2, "https://x/v4", "sk-mock", "glm-5.3"))
    check("429 → not recovered", r["success"], False)


def _mk_factory(session):
    factory = MagicMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    factory.return_value = cm
    return factory


def _orm_result(items):
    r = MagicMock()
    r.scalars.return_value.all.return_value = items
    return r


def _mk_recovery_env(status_code):
    """构造 quota_recovery_probe 的完整 mock 环境 (DB + cooldown_manager + http client)"""
    acc = MagicMock()
    acc.id, acc.channel_id, acc.api_key_ciphertext, acc.api_key = 2, 9, None, "sk-mock"
    ch = MagicMock()
    ch.id, ch.api_base, ch.base_url = 9, "https://x/api/coding/paas/v4", ""
    ps = MagicMock()
    ps.id, ps.channel_id, ps.model, ps.raw_model_id = 1, 9, "glm-5.3", "glm-5.3"
    session = MagicMock()
    session.execute = AsyncMock(side_effect=[
        _orm_result([acc]), _orm_result([ch]), _orm_result([ps])])
    session.add = MagicMock()
    session.commit = AsyncMock()
    client = MagicMock()
    client.post = AsyncMock(return_value=_mk_response(status_code))
    mock_cm = MagicMock()
    mock_cm.quota_buckets_due_for_probe.return_value = [(2, "glm-5.3")]
    return session, client, mock_cm


def test_quota_recovery_probe_success_clears_bucket():
    from intelligence import channel_health as ch_mod
    session, client, mock_cm = _mk_recovery_env(200)
    with patch.object(ch_mod, "async_session_factory", _mk_factory(session)), \
         patch.object(ch_mod, "cooldown_manager", mock_cm), \
         patch.object(ch_mod, "_get_probe_client", return_value=client), \
         patch("security.crypto.try_decrypt", side_effect=RuntimeError("no ciphertext")):
        recovered = asyncio.run(ch_mod.quota_recovery_probe())
    check("recovered=1", recovered, 1)
    mock_cm.quota_probe_result.assert_called_once_with(2, "glm-5.3", success=True)
    log = session.add.call_args.args[0]
    check("health log triggered_by", log.triggered_by, "quota_recovery")
    check("health log success", log.success, 1)


def test_quota_recovery_probe_failure_keeps_bucket():
    from intelligence import channel_health as ch_mod
    session, client, mock_cm = _mk_recovery_env(429)
    with patch.object(ch_mod, "async_session_factory", _mk_factory(session)), \
         patch.object(ch_mod, "cooldown_manager", mock_cm), \
         patch.object(ch_mod, "_get_probe_client", return_value=client):
        recovered = asyncio.run(ch_mod.quota_recovery_probe())
    check("recovered=0", recovered, 0)
    mock_cm.quota_probe_result.assert_called_once_with(2, "glm-5.3", success=False)


def test_quota_recovery_probe_noop_without_due_buckets():
    from intelligence import channel_health as ch_mod
    mock_cm = MagicMock()
    mock_cm.quota_buckets_due_for_probe.return_value = []
    with patch.object(ch_mod, "cooldown_manager", mock_cm):
        recovered = asyncio.run(ch_mod.quota_recovery_probe())
    check("no due → 0", recovered, 0)


# ===== 4. router 冷却/频限过滤告警 =====

class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.msgs = []

    def emit(self, record):
        self.msgs.append(record.getMessage())


def _build_channels():
    return [
        {"id": 5, "name": "sensenova_official", "provider": "sensenova",
         "priority": 5, "provider_type": "official",
         "billing_mode": "pay_per_token", "account_id": 51,
         "account_name": "sn-01", "api_key": "sk-mock",
         "quota_total": None, "quota_used": 0, "quota_remaining": None,
         "prices": [{"model": "deepseek-v4-pro", "price_type": "normal",
                     "time_range": "全天", "input_price": 0.0,
                     "output_price": 0.0}],
         "quotas": []},
        {"id": 1, "name": "deepseek_official", "provider": "deepseek",
         "priority": 10, "provider_type": "official",
         "billing_mode": "pay_per_token", "account_id": 52,
         "account_name": "ds-01", "api_key": "sk-mock",
         "quota_total": None, "quota_used": 0, "quota_remaining": None,
         "prices": [{"model": "deepseek-v4-pro", "price_type": "normal",
                     "time_range": "全天", "input_price": 3.0,
                     "output_price": 9.0}],
         "quotas": []},
    ]


def test_cooldown_filter_warning_reports_cooling_not_rate_limit():
    """冷却中的候选 → 告警报「冷却中 Ns」而非误导的「频限已满」"""
    from gateway.router import RouteEngine
    import gateway.cooldown as cd_mod
    engine = RouteEngine()
    engine._channels_cache = _build_channels()
    mock_cm = MagicMock()
    mock_cm.is_in_cooldown.side_effect = lambda aid, m=None: aid == 52
    mock_cm.cooldown_remaining.side_effect = lambda aid, m=None: 1234.0 if aid == 52 else 0.0
    capture = _Capture()
    rlog = logging.getLogger("gateway.router")
    rlog.addHandler(capture)
    old_cm = cd_mod.cooldown_manager
    cd_mod.cooldown_manager = mock_cm
    try:
        ch = engine.select_best_channel("deepseek-v4-pro",
                                        messages=[{"role": "user", "content": "hi"}])
        check("healthy channel still selected", ch["name"], "sensenova_official")
        mock_cm.is_in_cooldown.side_effect = lambda aid, m=None: True
        mock_cm.cooldown_remaining.side_effect = lambda aid, m=None: 1234.0
        ch2 = engine.select_best_channel("deepseek-v4-pro",
                                         messages=[{"role": "user", "content": "hi"}])
        check("all cooling → None", ch2, None)
        cooled = [m for m in capture.msgs if "冷却/频限过滤" in m]
        check("warning emitted", len(cooled) > 0, True)
        check("warning reports cooling account",
              any("deepseek_official/ds-01=冷却中1234s" in m for m in cooled), True)
        check("no misleading rate-limit wording",
              any("频限已满" not in m for m in cooled), True)
    finally:
        cd_mod.cooldown_manager = old_cm
        rlog.removeHandler(capture)


# ===== runner =====

def main():
    tests = [
        test_glm_reset_message_shortens_cooldown,
        test_reset_far_future_respected,
        test_no_reset_keyword_keeps_default,
        test_reset_keyword_without_time_keeps_default,
        test_past_reset_time_min_cooldown,
        test_invalid_datetime_keeps_default,
        test_english_reset_parses,
        test_quarantine_schedules_recovery_probe,
        test_probe_due_when_time_reached,
        test_probe_success_clears_bucket,
        test_probe_failure_backs_off_without_touching_count,
        test_probe_backoff_capped,
        test_needs_human_account_not_probed,
        test_expired_bucket_not_probed,
        test_cleanup_drops_orphan_probe_entries,
        test_cooldown_remaining_bucket_semantics,
        test_chat_probe_one_200_is_recovery,
        test_chat_probe_one_429_not_recovered,
        test_quota_recovery_probe_success_clears_bucket,
        test_quota_recovery_probe_failure_keeps_bucket,
        test_quota_recovery_probe_noop_without_due_buckets,
        test_cooldown_filter_warning_reports_cooling_not_rate_limit,
    ]
    for t in tests:
        print(f"\n--- {t.__name__} ---")
        try:
            t()
        except Exception as e:
            global _failed
            _failed += 1
            print(f"  EXCEPTION: {e}")
            import traceback
            traceback.print_exc()

    print(f"\n=== 结果: {_passed} passed, {_failed} failed ===")
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
