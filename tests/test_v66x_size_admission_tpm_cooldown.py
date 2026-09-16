"""V6.6.x: 路由体量准入 + TPM/RPM 类 429 冷却基数提升

背景 (2026-09-16 sensenova 直连探测 + 用户控制台截图):
- sensenova 免费公测池为全体用户共享吞吐, 大请求 (10 万 token 级) 基本必被
  429001 拒 (同秒 84 token 小请求可过), 每次白跑一趟上游再 failover;
  积分满格仍被拒 → 约束是共享吞吐不是积分
- 实测 63% 的 429 后面跟着 429, 完全静默 5 分钟也不恢复, 30s 冷却基数解冻即重撞

覆盖:
- _estimate_input_tokens: 纯文本 / 中文 / 图片 / 空 / 混合
- select_best_channel 体量准入: 大请求剔除超限渠道、小请求不受影响、
  全部候选超限时不 503 (保留原候选)、渠道不在 map 中不受限、messages=None 跳过
- classify: 429001 / tpm/rpm 措辞 → rate_limited 300s 基数; 普通 429 保持 30s
- CooldownManager: 429001 失败序列 300→600→1200 指数退避 (封顶不变)

跑法:
    python tests/test_v66x_size_admission_tpm_cooldown.py
    或 pytest tests/test_v66x_size_admission_tpm_cooldown.py -v
"""
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _install_mock_config():
    """mock config.settings 让 router.py 能 import (跟 test_null_price_routing 一致)"""
    import types

    class _MockSettings:
        aimc_master_key = "0" * 64
        channel_max_input_tokens: dict = {}

    sys.modules.setdefault("config", types.ModuleType("config"))
    sys.modules["config"].settings = _MockSettings()


_install_mock_config()

from config import settings  # noqa: E402
from gateway.error_classifier import ErrorCategory, classify  # noqa: E402
from gateway.router import RouteEngine, _estimate_input_tokens  # noqa: E402

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


# ====================== 1. _estimate_input_tokens ======================

def test_estimate_pure_ascii():
    """英文 ~4 字符/token, 3600 字符 → 1200"""
    msgs = [{"role": "user", "content": "a" * 3600}]
    check("estimate ascii 3600 chars", _estimate_input_tokens(msgs), 1200)


def test_estimate_chinese():
    """中文 ~1.5 字符/token, 300 汉字 → 100 (按 3 字符/token 口径)"""
    msgs = [{"role": "user", "content": "汉" * 300}]
    check("estimate chinese 300 chars", _estimate_input_tokens(msgs), 100)


def test_estimate_images():
    """图片每张按 1000 token, 无文本 → 2000"""
    msgs = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,xxx"}},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,yyy"}},
    ]}]
    check("estimate 2 images", _estimate_input_tokens(msgs), 2000)


def test_estimate_mixed_parts():
    """content 列表: text 部分 + 图片"""
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "x" * 900},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,zzz"}},
    ]}]
    check("estimate text+image", _estimate_input_tokens(msgs), 1300)


def test_estimate_empty():
    check("estimate None", _estimate_input_tokens(None), 0)
    check("estimate empty list", _estimate_input_tokens([]), 0)


# ====================== 2. select_best_channel 体量准入 ======================

def _build_channels(names):
    """sensenova (0 价, priority 5) + deepseek_official (3 元, priority 10)"""
    pool = {
        "sensenova_official": {
            "id": 5, "name": "sensenova_official", "provider": "sensenova",
            "priority": 5, "provider_type": "official",
            "billing_mode": "pay_per_token", "account_id": 51,
            "account_name": "sn-01", "api_key": "sk-mock",
            "quota_total": None, "quota_used": 0, "quota_remaining": None,
            "prices": [{"model": "deepseek-v4-pro", "price_type": "normal",
                        "time_range": "全天", "input_price": 0.0,
                        "output_price": 0.0}],
            "quotas": [],
        },
        "deepseek_official": {
            "id": 1, "name": "deepseek_official", "provider": "deepseek",
            "priority": 10, "provider_type": "official",
            "billing_mode": "pay_per_token", "account_id": 52,
            "account_name": "ds-01", "api_key": "sk-mock",
            "quota_total": None, "quota_used": 0, "quota_remaining": None,
            "prices": [{"model": "deepseek-v4-pro", "price_type": "normal",
                        "time_range": "全天", "input_price": 3.0,
                        "output_price": 9.0}],
            "quotas": [],
        },
    }
    return [pool[n] for n in names]


def _select(names, messages):
    engine = RouteEngine()
    engine._channels_cache = _build_channels(names)
    return engine.select_best_channel("deepseek-v4-pro", messages=messages)


def test_big_request_skips_sensenova():
    """10 万字符 (~33k token 估算) > 30000 → 剔除 sensenova, 选 deepseek_official"""
    old = getattr(settings, "channel_max_input_tokens", None)
    settings.channel_max_input_tokens = {"sensenova_official": 30000}
    try:
        ch = _select(["sensenova_official", "deepseek_official"],
                     [{"role": "user", "content": "x" * 120000}])
        check("big request skips sensenova", ch["name"], "deepseek_official")
    finally:
        settings.channel_max_input_tokens = old


def test_small_request_keeps_sensenova():
    """小请求不触发准入, 0 价 sensenova 仍最便宜首选"""
    old = getattr(settings, "channel_max_input_tokens", None)
    settings.channel_max_input_tokens = {"sensenova_official": 30000}
    try:
        ch = _select(["sensenova_official", "deepseek_official"],
                     [{"role": "user", "content": "hi"}])
        check("small request keeps sensenova", ch["name"], "sensenova_official")
    finally:
        settings.channel_max_input_tokens = old


def test_boundary_exact_limit_allowed():
    """估算恰好等于阈值 (90000 字符 → 30000) → 不剔除 (只剔 strictly over)"""
    old = getattr(settings, "channel_max_input_tokens", None)
    settings.channel_max_input_tokens = {"sensenova_official": 30000}
    try:
        ch = _select(["sensenova_official", "deepseek_official"],
                     [{"role": "user", "content": "x" * 90000}])
        check("est == limit still allowed", ch["name"], "sensenova_official")
    finally:
        settings.channel_max_input_tokens = old


def test_empty_map_disables_gate():
    """map 为空 → 准入关闭, 大请求仍走 0 价 sensenova (行为回退开关)"""
    old = getattr(settings, "channel_max_input_tokens", None)
    settings.channel_max_input_tokens = {}
    try:
        ch = _select(["sensenova_official", "deepseek_official"],
                     [{"role": "user", "content": "x" * 120000}])
        check("empty map disables gate", ch["name"], "sensenova_official")
    finally:
        settings.channel_max_input_tokens = old


def test_all_over_limit_keeps_candidates():
    """只有 sensenova 一个候选且超限 → 保留原候选 (不 503, 宁试一次)"""
    old = getattr(settings, "channel_max_input_tokens", None)
    settings.channel_max_input_tokens = {"sensenova_official": 30000}
    try:
        ch = _select(["sensenova_official"],
                     [{"role": "user", "content": "x" * 120000}])
        check("all over limit keeps candidate", ch["name"], "sensenova_official")
    finally:
        settings.channel_max_input_tokens = old


def test_channel_not_in_map_unaffected():
    """渠道不在 map 中 → 不受体量准入限制"""
    old = getattr(settings, "channel_max_input_tokens", None)
    settings.channel_max_input_tokens = {"sensenova_official": 30000}
    try:
        ch = _select(["deepseek_official"],
                     [{"role": "user", "content": "x" * 120000}])
        check("channel not in map unaffected", ch["name"], "deepseek_official")
    finally:
        settings.channel_max_input_tokens = old


def test_none_messages_skips_gate():
    """messages=None (无法估算) → 跳过准入, 保持原行为"""
    old = getattr(settings, "channel_max_input_tokens", None)
    settings.channel_max_input_tokens = {"sensenova_official": 30000}
    try:
        engine = RouteEngine()
        engine._channels_cache = _build_channels(["sensenova_official"])
        ch = engine.select_best_channel("deepseek-v4-pro", messages=None)
        check("None messages skips gate", ch["name"], "sensenova_official")
    finally:
        settings.channel_max_input_tokens = old


# ====================== 3. 429001 冷却基数 ======================

def test_classify_429001_tpm_pool():
    """sensenova 429001 (tpm/rpm 措辞) → rate_limited 但基数 300s"""
    info = classify(429, '{"error":{"code":"429001","message":"inference exceeds tpm/rpm limit"}}')
    check("429001 category", info.category, ErrorCategory.RATE_LIMITED)
    check("429001 cooldown", info.suggested_cooldown_seconds, 300)
    check("429001 probe_after", info.suggested_probe_after_seconds, 600)
    check("429001 needs_human", info.needs_human, False)


def test_classify_rpm_exhausted_code8():
    """code 8 / rpm exhausted → 命中 rpm 措辞, 同样 300s 基数"""
    info = classify(429, '{"error":{"code":"8","type":"quota_exceeded_error","message":"rpm exhausted"}}')
    check("rpm exhausted category", info.category, ErrorCategory.RATE_LIMITED)
    check("rpm exhausted cooldown", info.suggested_cooldown_seconds, 300)


def test_classify_plain_429_unchanged():
    """普通瞬时 429 (无 tpm/rpm 措辞) → 保持 30s/60s 原行为"""
    info = classify(429, '{"error":{"message":"Too Many Requests, slow down"}}')
    check("plain 429 category", info.category, ErrorCategory.RATE_LIMITED)
    check("plain 429 cooldown", info.suggested_cooldown_seconds, 30)
    check("plain 429 probe_after", info.suggested_probe_after_seconds, 60)


def test_cooldown_backoff_sequence_429001():
    """429001 连续失败: 冷却 300→600→1200 (指数退避, 首败即基准)"""
    from gateway.cooldown import CooldownManager
    cm = CooldownManager(failure_threshold=1, max_probe_backoff_seconds=3600)
    info = classify(429, '{"error":{"code":"429001","message":"inference exceeds tpm/rpm limit"}}')
    got = []
    for _ in range(3):
        t0 = time.time()
        cm.mark_failure(9001, error_info=info)  # 同一账号连续失败, count 递增
        until = cm._state[9001]["cooldown_until"]
        got.append(round(until - t0))
    check("1st failure cooldown ~300s", 295 <= got[0] <= 305, True)
    check("2nd failure cooldown ~600s", 595 <= got[1] <= 605, True)
    check("3rd failure cooldown ~1200s", 1195 <= got[2] <= 1205, True)


def test_cooldown_backoff_plain_429_unchanged():
    """普通 429 连续失败: 30→60→120 原序列不变"""
    from gateway.cooldown import CooldownManager
    cm = CooldownManager(failure_threshold=1, max_probe_backoff_seconds=3600)
    info = classify(429, '{"error":{"message":"Too Many Requests, slow down"}}')
    got = []
    for _ in range(3):
        t0 = time.time()
        cm.mark_failure(9101, error_info=info)
        until = cm._state[9101]["cooldown_until"]
        got.append(round(until - t0))
    check("plain 1st ~30s", 25 <= got[0] <= 35, True)
    check("plain 2nd ~60s", 55 <= got[1] <= 65, True)
    check("plain 3rd ~120s", 115 <= got[2] <= 125, True)


# ====================== runner ======================

if __name__ == "__main__":
    import traceback

    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
    for name, fn in tests:
        print(f"--- {name} ---")
        try:
            fn()
        except Exception:
            _failed += 1
            print(f"  FAIL  {name} raised:")
            traceback.print_exc()
    print(f"\n===== {_passed} passed, {_failed} failed =====")
    sys.exit(1 if _failed else 0)
