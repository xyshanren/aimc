# -*- coding: utf-8 -*-
"""V6.10 taotoken 双模型桶均衡 — cooldown 模型级隔离 + 大请求收敛 + 组路由 LRU 轮换

覆盖:
1. CooldownManager._quarantine: quota_exhausted + model → 只封本桶, 兄弟桶可用;
   legacy 无 model 调用账号级语义不变; mark_success(model) 只清本桶
2. select_best_channel 大请求 taotoken 优先收敛; 小请求不受影响
3. _select_by_group 同账号双桶 LRU 轮换: 连续大请求 deepseek/glm 交替;
   桶隔离时对方接棒; 单桶/小请求不轮换

跑法: python -m pytest tests/test_taotoken_balance.py -v
"""

import importlib
import sys
import types
from pathlib import Path

ROOT = Path(__file__).parent.parent.resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _install_mock_env():
    """mock config.settings (gateway.router 模块顶层 from config import settings)"""
    sys.modules.setdefault("config", types.ModuleType("config"))
    sys.modules["config"].settings = types.SimpleNamespace(
        openai_api_key="sk-test",
        openai_base_url="https://mock.local/v1",
        parse_model="gpt-4o-mini",
        parse_confidence_threshold=0.6,
        crawl_enabled=True,
        aimc_master_key="a" * 64,
        route_refresh_interval=600,
        db_url="sqlite+aiosqlite:///./_v610_test.db",
        db_path="./_v610_test.db",
        host="0.0.0.0",
        port=8080,
        log_level="info",
        request_timeout=30,
        # V6.6.x 体量准入窗 (与 config.py 默认一致): 小请求→sensenova, 大请求→taotoken
        channel_min_input_tokens={"taotoken_coding": 30000},
        channel_max_input_tokens={"sensenova_official": 30000},
    )


_install_mock_env()


# ============================================================
# 1. cooldown 模型桶级隔离 (quarantine)
# ============================================================

def _quota_info():
    from gateway.error_classifier import ErrorCategory, ErrorInfo
    return ErrorInfo(
        category=ErrorCategory.QUOTA_EXHAUSTED,
        error_code="insufficient_quota",
        error_msg="deepseek_for_coding requires active Coding Plan quota",
        needs_human=True,
        suggested_cooldown_seconds=3600,
        suggested_probe_after_seconds=1800,
    )


def test_quarantine_blocks_only_that_model_bucket():
    """quota_exhausted + model → 只封该桶; 兄弟模型桶与账号级查询不受影响"""
    from gateway.cooldown import CooldownManager

    mgr = CooldownManager()
    mgr.mark_failure(99901, error_info=_quota_info(), model="deepseek-v4-flash")

    assert mgr.is_in_cooldown(99901, model="deepseek-v4-flash"), "deepseek 桶应被封"
    assert not mgr.is_in_cooldown(99901, model="glm-5.3-flash"), "glm 桶应可继续路由"
    assert not mgr.is_in_cooldown(99901), "账号级不应被卷进冷却 (旧语义: 账号级冷却会拖死兄弟桶)"

    # snapshot 携带桶隔离信息 (运维卡可观察)
    snap = mgr.snapshot().get(99901, {})
    assert snap.get("quarantine_models", {}).get("deepseek-v4-flash", 0) > 0


def test_quarantine_mark_success_clears_only_own_bucket():
    """mark_success(model) 只清本桶 — glm 桶 200 不应擦掉 deepseek 桶冷却"""
    from gateway.cooldown import CooldownManager

    mgr = CooldownManager()
    mgr.mark_failure(99902, error_info=_quota_info(), model="deepseek-v4-flash")
    mgr.mark_success(99902, model="glm-5.3-flash")  # 兄弟桶成功

    assert mgr.is_in_cooldown(99902, model="deepseek-v4-flash"), "deepseek 桶冷却应保留"
    mgr.mark_success(99902, model="deepseek-v4-flash")
    assert not mgr.is_in_cooldown(99902, model="deepseek-v4-flash"), "本桶成功后清除"


def test_quarantine_legacy_account_scope_unchanged():
    """无 model 调用保持旧账号级语义: quota 失败封整账号, is_in_cooldown(acc) 生效"""
    from gateway.cooldown import CooldownManager

    mgr = CooldownManager()
    mgr.mark_failure(99903, error_info=_quota_info())  # 无 model (旧调用方)
    assert mgr.is_in_cooldown(99903)
    assert mgr.is_in_cooldown(99903, model="deepseek-v4-flash"), "账号级冷却对带 model 查询同样生效"

    # legacy 失败 (无 error_info) 仍走固定 60s 账号级
    mgr2 = CooldownManager()
    mgr2.mark_failure(99904, reason="HTTP 401")
    assert mgr2.is_in_cooldown(99904)


def test_quarantine_needs_human_escalation_kept():
    """桶级失败仍走账号级 needs_human 升级 (触顶 2 次 → 告警可观测)"""
    from gateway.cooldown import CooldownManager

    mgr = CooldownManager()
    mgr.mark_failure(99905, error_info=_quota_info(), model="deepseek-v4-flash")
    assert not mgr.needs_human(99905)
    mgr.mark_failure(99905, error_info=_quota_info(), model="deepseek-v4-flash")
    assert mgr.needs_human(99905), "同一桶第 2 次 quota 失败应升级 needs_human"


def test_quarantine_cleanup_removes_expired():
    """cleanup() 只清过期的桶隔离; 未过期保留 (与账号级 count 保留窗一致)"""
    import time
    from gateway.cooldown import CooldownManager

    mgr = CooldownManager()
    mgr.mark_failure(99906, error_info=_quota_info(), model="deepseek-v4-flash")
    assert mgr.cleanup() == 0, "3600s 未过期的隔离应保留"
    assert mgr.is_in_cooldown(99906, model="deepseek-v4-flash")

    # 推进到过期 → cleanup 移除
    mgr._quarantine[(99906, "deepseek-v4-flash")] = time.time() - 1
    assert mgr.cleanup() == 1
    assert not mgr.is_in_cooldown(99906, model="deepseek-v4-flash")


# ============================================================
# 2/3. router: 大请求收敛 + 组路由轮换
# ============================================================

def _make_engine():
    """独立 RouteEngine + 注入 taotoken/glm_bigmodel/sensenova 缓存与 tier:strong 组。

    复用 test_v46_cooldown 的 dict 形状 (channel entry = 账号粒度, prices 全量)。
    """
    import gateway.router as router_mod
    engine = router_mod.RouteEngine()
    engine._cards_cache = {}

    taotoken = {
        "id": 8, "name": "taotoken_coding", "provider": "taotoken",
        "base_url": "https://taotoken.mock/v1", "priority": 9,
        "account_id": 12, "account_name": "taotoken_02", "api_key": "sk-tt",
        "quota_total": None, "quota_used": 0, "quota_remaining": None,
        "prices": [
            {"model": "deepseek-v4-flash", "price_type": "pay_per_token",
             "time_range": "全天", "input_price": 0.0, "output_price": 0.0},
            {"model": "glm-5.3-flash", "price_type": "pay_per_token",
             "time_range": "全天", "input_price": 0.0, "output_price": 0.0},
        ],
        "quotas": [], "billing_mode": "pay_per_token",
    }
    glm_bigmodel = {
        "id": 2, "name": "glm_bigmodel", "provider": "glm",
        "base_url": "https://glm.mock/v1", "priority": 8,
        "account_id": 2, "account_name": "glm_01", "api_key": "sk-glm",
        "quota_total": None, "quota_used": 0, "quota_remaining": None,
        "prices": [
            {"model": "glm-5.3-flash", "price_type": "pay_per_token",
             "time_range": "全天", "input_price": 0.0, "output_price": 0.0},
        ],
        "quotas": [], "billing_mode": "pay_per_token",
    }
    sensenova = {
        "id": 5, "name": "sensenova_official", "provider": "sensenova",
        "base_url": "https://sn.mock/v1", "priority": 10,
        "account_id": 3, "account_name": "sensenova_01", "api_key": "sk-sn",
        "quota_total": None, "quota_used": 0, "quota_remaining": None,
        "prices": [
            {"model": "deepseek-v4-flash", "price_type": "pay_per_token",
             "time_range": "全天", "input_price": 0.0, "output_price": 0.0},
        ],
        "quotas": [], "billing_mode": "pay_per_token",
    }
    engine._channels_cache = [taotoken, glm_bigmodel, sensenova]

    engine._registry_cache = {
        "deepseek-v4-flash": types.SimpleNamespace(
            model_id="deepseek-v4-flash", family="deepseek", tier="T1",
            modality="text", context_length=128000, capabilities=None),
        "glm-5.3-flash": types.SimpleNamespace(
            model_id="glm-5.3-flash", family="glm", tier="T2",
            modality="multimodal", context_length=128000, capabilities=None),
    }
    engine._groups_cache = {
        "tier:strong": types.SimpleNamespace(
            group_id="tier:strong", display_name="主力", enabled=True, tier=None,
            capabilities=None, min_context=0, require_vision=False,
            strategy="cheapest", fallback_group="tier:balanced"),
    }
    engine._group_members_cache = {
        "tier:strong": ["deepseek-v4-flash", "glm-5.3-flash"],
    }
    engine._member_priority_cache = {
        "tier:strong": {"deepseek-v4-flash": 100, "glm-5.3-flash": 120},
    }
    engine._score_weights = {
        "W_PRICE": 0.40, "W_QUOTA": 0.30, "W_SUCCESS": 0.20,
        "W_RECENCY": 0.05, "W_PRIORITY": 0.05,
    }
    return engine


BIG_MSGS = [{"role": "user", "content": "x" * 120000}]      # est ≈ 40000 ≥ 30000
SMALL_MSGS = [{"role": "user", "content": "hi what is 1+1?"}]


def test_big_request_converges_to_taotoken():
    """直连大请求: select_best_channel 收敛到 taotoken (glm 0 元 + sensenova max 窗剔除后)"""
    engine = _make_engine()
    best = engine.select_best_channel("glm-5.3-flash", messages=BIG_MSGS)
    assert best is not None
    assert best["name"] == "taotoken_coding", \
        f"大请求应收敛 taotoken, 实际 {best['name']}"
    # 小请求: taotoken 被 min 窗剔除 → glm_bigmodel
    small = engine.select_best_channel("glm-5.3-flash", messages=SMALL_MSGS)
    assert small["name"] == "glm_bigmodel", f"小请求应走 glm_bigmodel, 实际 {small['name']}"


def test_group_rotation_alternates_models():
    """同账号双桶 LRU 轮换: 连续大请求 deepseek → glm → deepseek"""
    engine = _make_engine()
    # 清掉 cooldown singleton 残留
    from gateway.cooldown import cooldown_manager
    cooldown_manager.reset(12)

    first = engine.select_channel("tier:strong", messages=BIG_MSGS)
    assert first is not None
    m1 = first.get("_registry_model") or first.get("_actual_model")
    assert first["name"] == "taotoken_coding", f"组路由大请求应落 taotoken, 实际 {first['name']}"

    second = engine.select_channel("tier:strong", messages=BIG_MSGS)
    m2 = second.get("_registry_model") or second.get("_actual_model")

    third = engine.select_channel("tier:strong", messages=BIG_MSGS)
    m3 = third.get("_registry_model") or third.get("_actual_model")

    assert m1 in ("deepseek-v4-flash", "glm-5.3-flash"), f"未知成员 {m1}"
    assert m2 != m1, f"第 2 次应轮换到另一模型桶, 实际仍为 {m1}"
    assert m3 == m1, f"第 3 次应回到 {m1}, 实际 {m3}"


def test_group_rotation_respects_quarantine():
    """glm 桶隔离中 → 大请求全部由 deepseek@taotoken 承接 (对方桶接棒)"""
    from gateway.cooldown import cooldown_manager
    cooldown_manager.reset(12)
    cooldown_manager.mark_failure(12, error_info=_quota_info(), model="glm-5.3-flash")
    assert cooldown_manager.is_in_cooldown(12, model="glm-5.3-flash")

    engine = _make_engine()
    for _ in range(3):
        best = engine.select_channel("tier:strong", messages=BIG_MSGS)
        assert best is not None
        m = best.get("_registry_model") or best.get("_actual_model")
        assert m == "deepseek-v4-flash", f"glm 桶隔离期间应全部 deepseek, 实际 {m}"
        assert best["name"] == "taotoken_coding", "仍是 taotoken 承担 (不再 fallback glm_bigmodel)"

    # 隔离过期 → 恢复轮换
    cooldown_manager.reset(12)


def test_small_group_request_no_rotation():
    """小请求: taotoken 不在候选 (min 窗), 无轮换, 走原 cheapest (deepseek 成员优先)"""
    engine = _make_engine()
    from gateway.cooldown import cooldown_manager
    cooldown_manager.reset(12)

    for _ in range(3):
        best = engine.select_channel("tier:strong", messages=SMALL_MSGS)
        assert best is not None
        assert best["name"] != "taotoken_coding", "小请求不应进 taotoken"