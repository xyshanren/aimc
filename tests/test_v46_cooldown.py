"""V4.6.1 CooldownManager + 路由集成单元测试

覆盖:
1. CooldownManager 基础 (mark_failure → cooldown, mark_success → reset, is_in_cooldown, cleanup)
2. _update_cooldown hook (proxy.py): 200 → mark_success / 401/403/5xx → mark_failure / 其他不动
3. _is_channel_usable (router.py): 冷却期内的 channel 被路由跳过

跑法: python tests/test_v46_cooldown.py
"""

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ============================================================
# Mock config + 临时 SQLite
# ============================================================
def _install_mock_env():
    tmp_dir = tempfile.mkdtemp(prefix="aimc_v46_cooldown_")
    db_path = os.path.join(tmp_dir, "test.db")
    db_url = f"sqlite+aiosqlite:///{db_path}"

    import types

    sys.modules.setdefault("config", types.ModuleType("config"))
    sys.modules["config"].settings = SimpleNamespace(
        openai_api_key="sk-test",
        openai_base_url="https://mock.local/v1",
        parse_model="gpt-4o-mini",
        parse_confidence_threshold=0.6,
        crawl_enabled=True,
        aimc_master_key="a" * 64,
        route_refresh_interval=600,
        db_url=db_url,
        db_path=db_path,
        host="0.0.0.0",
        port=8080,
        log_level="info",
        request_timeout=30,
    )


_install_mock_env()


# ============================================================
# Test 1: mark_failure 触发冷却
# ============================================================
def test_cooldown_mark_failure_triggers():
    print("\n=== Test 1: mark_failure 触发冷却 ===")
    from gateway.cooldown import CooldownManager

    mgr = CooldownManager(cooldown_seconds=60, failure_threshold=1)
    assert not mgr.is_in_cooldown(101)
    assert mgr.cooldown_remaining(101) == 0.0

    mgr.mark_failure(101, reason="HTTP 401")
    assert mgr.is_in_cooldown(101), "1 次失败 (threshold=1) 应立即进入冷却"
    remaining = mgr.cooldown_remaining(101)
    assert 0 < remaining <= 60, f"剩余冷却应在 (0, 60], 实际 {remaining}"
    print(f"  [OK] mark_failure 后: is_in_cooldown=True, remaining={remaining:.2f}s")


# ============================================================
# Test 2: mark_success 清冷却
# V6.4.x 语义更新: 冷却期内的"迟到成功"不再清冷却 (在途长流式请求完成
# 不证明账号当前健康, 生产实证 429 冷却被 2s 后完成的 200 擦掉);
# 冷却过期后的成功仍全量复位
# ============================================================
def test_cooldown_mark_success_resets():
    print("\n=== Test 2: mark_success 清冷却 ===")
    from gateway.cooldown import CooldownManager

    mgr = CooldownManager(cooldown_seconds=60, failure_threshold=1)
    mgr.mark_failure(202)
    assert mgr.is_in_cooldown(202)

    mgr.mark_success(202)
    assert mgr.is_in_cooldown(202), "冷却期内的迟到成功不应清掉 cooldown (V6.4.x)"

    # 模拟冷却过期后再成功 → 全量复位
    mgr._state[202]["cooldown_until"] = time.time() - 1
    mgr.mark_success(202)
    assert not mgr.is_in_cooldown(202), "冷却过期后 mark_success 应清掉 cooldown"
    assert mgr.cooldown_remaining(202) == 0.0
    print(f"  [OK] 冷却内迟到成功保留冷却 / 冷却过期后成功复位")


# ============================================================
# Test 3: is_in_cooldown 自动清过期 entry
# ============================================================
def test_cooldown_auto_clear_expired():
    print("\n=== Test 3: 过期自动解除冷却 ===")
    from gateway.cooldown import CooldownManager

    mgr = CooldownManager(cooldown_seconds=1, failure_threshold=1)
    mgr.mark_failure(303)
    assert mgr.is_in_cooldown(303)

    time.sleep(1.1)  # 等冷却过期
    # 下一次 is_in_cooldown 应判定过期
    assert not mgr.is_in_cooldown(303), "1.1s 后冷却应过期"
    # V6.5: 过期不再 pop 整个 state — count 承载指数退避序列, 只标记过期
    # (cooldown_until=None + cooldown_expired_at), 物理清理交给 cleanup 保留窗
    snap = mgr.snapshot()[303]
    assert snap["cooldown_until"] is None
    assert snap["in_cooldown"] is False
    assert 303 in mgr._state and mgr._state[303]["cooldown_expired_at"] is not None
    print(f"  [OK] 1s 冷却 + sleep 1.1s → 冷却解除, state 保留 (V6.5 退避延续)")


# ============================================================
# Test 4: cleanup 批量清理
# ============================================================
def test_cooldown_cleanup():
    print("\n=== Test 4: cleanup 批量清理 ===")
    from gateway.cooldown import CooldownManager

    mgr = CooldownManager(cooldown_seconds=1, failure_threshold=1)
    mgr.mark_failure(1)
    mgr.mark_failure(2)
    mgr.mark_failure(3)
    time.sleep(1.1)
    # V6.5: 过期未超保留窗不清 (count 供退避延续); 拨过保留窗后才清
    assert mgr.cleanup() == 0, "保留窗内 cleanup 不得清"
    for aid in (1, 2, 3):
        mgr._state[aid]["cooldown_until"] = None  # 模拟 is_in_cooldown 查询的过期标记
        mgr._state[aid]["cooldown_expired_at"] = time.time() - 700
    removed = mgr.cleanup()
    assert removed == 3, f"超保留窗后 cleanup 应清掉 3 个, 实际 {removed}"
    assert mgr.snapshot() == {}, "清理后 _state 应空"
    print(f"  [OK] cleanup 保留窗外一次清掉 3 个过期 entry")


# ============================================================
# Test 5: reset 强制清空 (admin 用)
# ============================================================
def test_cooldown_reset():
    print("\n=== Test 5: reset 强制清空 ===")
    from gateway.cooldown import CooldownManager

    mgr = CooldownManager()
    mgr.mark_failure(404)
    mgr.mark_failure(405)
    assert mgr.is_in_cooldown(404)

    mgr.reset(404)
    assert not mgr.is_in_cooldown(404)
    # 不影响其他账号
    assert mgr.is_in_cooldown(405)
    print(f"  [OK] reset(404) → 404 清, 405 仍在冷却")


# ============================================================
# Test 6: failure_threshold > 1 时需要累计
# ============================================================
def test_cooldown_threshold_accumulation():
    print("\n=== Test 6: 阈值累计 (threshold=3) ===")
    from gateway.cooldown import CooldownManager

    mgr = CooldownManager(cooldown_seconds=60, failure_threshold=3)
    mgr.mark_failure(606)  # 第 1 次
    assert not mgr.is_in_cooldown(606), "1/3 次失败不应触发冷却"

    mgr.mark_failure(606)  # 第 2 次
    assert not mgr.is_in_cooldown(606), "2/3 次失败不应触发冷却"

    mgr.mark_failure(606)  # 第 3 次, 达到阈值
    assert mgr.is_in_cooldown(606), "3/3 次失败应触发冷却"
    print(f"  [OK] 阈值 3 时累计 3 次失败才进入冷却")


# ============================================================
# Test 7: snapshot 是深拷贝 (不暴露内部引用)
# ============================================================
def test_cooldown_snapshot_isolated():
    print("\n=== Test 7: snapshot 深拷贝隔离 ===")
    from gateway.cooldown import CooldownManager

    mgr = CooldownManager()
    mgr.mark_failure(707)
    snap = mgr.snapshot()
    assert 707 in snap
    # 修改 snapshot 不应影响原 state
    snap[707]["count"] = 999
    assert mgr.snapshot()[707]["count"] != 999, "snapshot 应是深拷贝"
    print(f"  [OK] snapshot 改了 count, 内部 state 不变")


# ============================================================
# Test 8: _update_cooldown hook (proxy.py)
# ============================================================
def test_proxy_update_cooldown_hook():
    print("\n=== Test 8: proxy._update_cooldown hook ===")
    from unittest.mock import MagicMock
    import importlib

    # 重置 cooldown_manager 单例, 避免被前面 test 污染
    import gateway.cooldown as cooldown_mod
    importlib.reload(cooldown_mod)
    import gateway.proxy as proxy_mod
    importlib.reload(proxy_mod)

    mgr = cooldown_mod.cooldown_manager
    mgr.reset(999)  # 清干净

    channel = {"account_id": 999, "name": "test_ch"}

    # 1) 200 → mark_success
    proxy_mod._update_cooldown(channel, 200)
    assert 999 not in mgr.snapshot(), "200 应清空 entry"

    # 2) 401 → mark_failure (进入冷却)
    proxy_mod._update_cooldown(channel, 401)
    assert mgr.is_in_cooldown(999), "401 应触发冷却"

    # 3) 200 → mark_success (恢复)
    # V6.4.x: 冷却期内的迟到成功不再清冷却; 冷却外的 200 无冷却可清,
    # 用 reset 模拟"冷却已结束"后验证 200 分支不误标失败
    mgr.reset(999)
    proxy_mod._update_cooldown(channel, 200)
    assert not mgr.is_in_cooldown(999), "冷却外 200 应走 mark_success, 不进冷却"

    # 4) 400 → 不动 (4xx 客户端错误, 不应让账号冷却)
    mgr.reset(999)
    proxy_mod._update_cooldown(channel, 400)
    assert 999 not in mgr.snapshot(), "400 不应触发 mark_failure"
    assert not mgr.is_in_cooldown(999)

    # 5) 502 → mark_failure
    proxy_mod._update_cooldown(channel, 502)
    assert mgr.is_in_cooldown(999), "502 应触发冷却"

    # 6) channel 没 account_id → 静默跳过
    no_acc_channel = {"name": "broken"}
    proxy_mod._update_cooldown(no_acc_channel, 500)  # 不应抛异常
    print(f"  [OK] 200/401/400/502/account_id 缺失 各分支")


# ============================================================
# Test 9: _is_channel_usable 跳过冷却期账号 (router.py)
# ============================================================
def test_router_is_channel_usable_skips_cooldown():
    print("\n=== Test 9: _is_channel_usable 跳过冷却 ===")
    import importlib
    import gateway.cooldown as cooldown_mod
    importlib.reload(cooldown_mod)
    import gateway.limiter as limiter_mod  # noqa: F401
    importlib.reload(limiter_mod)
    import gateway.router as router_mod
    importlib.reload(router_mod)

    mgr = cooldown_mod.cooldown_manager
    mgr.reset(808)

    # 模拟一个 channel: account_id=808
    ch = {
        "account_id": 808,
        "quotas": [],  # limiter.is_limited(808, []) -> False
        "quota_remaining": None,
    }

    engine = router_mod.RouteEngine()
    # 1) 正常状态: usable
    assert engine._is_channel_usable(ch), "未冷却时应 usable"

    # 2) 进冷却后: unusable
    mgr.mark_failure(808, reason="test")
    assert not engine._is_channel_usable(ch), "冷却中应 unusable"

    # 3) mark_success 后: 恢复
    # V6.4.x: 冷却期内迟到成功不清冷却, 先模拟冷却过期再验证复位
    mgr._state[808]["cooldown_until"] = time.time() - 1
    mgr.mark_success(808)
    assert engine._is_channel_usable(ch), "冷却过期后 mark_success 应 usable"
    print(f"  [OK] 未冷却→usable / 冷却中→unusable / mark_success→usable")


# ============================================================
# Test 10: router.least_used 排序 (同价 + least-used 优先)
# ============================================================
def test_router_least_used_round_robin():
    print("\n=== Test 10: least-used 轮询 (同价时老账号优先) ===")
    import importlib
    import gateway.router as router_mod
    importlib.reload(router_mod)

    # 两个 channel 价格完全一样: a (account=1) vs b (account=2)
    ch_a = {
        "id": 1, "name": "a", "provider": "test", "base_url": "https://a",
        "priority": 100, "account_id": 1, "account_name": "a1", "api_key": "sk-a",
        "quota_total": None, "quota_used": 0, "quota_remaining": None,
        "prices": [{"model": "m", "price_type": "pay_per_token",
                    "time_range": "全天", "input_price": 1.0, "output_price": 1.0}],
        "quotas": [], "billing_mode": "pay_per_token",
    }
    ch_b = {
        "id": 2, "name": "b", "provider": "test", "base_url": "https://b",
        "priority": 100, "account_id": 2, "account_name": "b1", "api_key": "sk-b",
        "quota_total": None, "quota_used": 0, "quota_remaining": None,
        "prices": [{"model": "m", "price_type": "pay_per_token",
                    "time_range": "全天", "input_price": 1.0, "output_price": 1.0}],
        "quotas": [], "billing_mode": "pay_per_token",
    }

    engine = router_mod.RouteEngine()
    engine._channels_cache = [ch_a, ch_b]
    engine._cards_cache = {}

    # 第 1 次: 都没用过, 按列表顺序 a 先
    first_pick = engine.select_best_channel("m")
    assert first_pick is not None
    first_acc = first_pick["account_id"]
    print(f"  第 1 次 pick: account_id={first_acc}")

    # 第 2 次: 应换另一个 (上一次用的 last_used 较新)
    second_pick = engine.select_best_channel("m")
    second_acc = second_pick["account_id"]
    print(f"  第 2 次 pick: account_id={second_acc}")
    assert second_acc != first_acc, f"least-used 应轮询, 但连续 2 次都选了 {first_acc}"

    # 第 3 次: 应回到 first
    third_pick = engine.select_best_channel("m")
    third_acc = third_pick["account_id"]
    print(f"  第 3 次 pick: account_id={third_acc}")
    assert third_acc == first_acc, f"第 3 次应回到首次 (轮询), 实际 {third_acc}"
    print(f"  [OK] 三次轮询: {first_acc} → {second_acc} → {third_acc}")


def main():
    tests = [
        test_cooldown_mark_failure_triggers,
        test_cooldown_mark_success_resets,
        test_cooldown_auto_clear_expired,
        test_cooldown_cleanup,
        test_cooldown_reset,
        test_cooldown_threshold_accumulation,
        test_cooldown_snapshot_isolated,
        test_proxy_update_cooldown_hook,
        test_router_is_channel_usable_skips_cooldown,
        test_router_least_used_round_robin,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as e:
            print(f"\n[FAIL] {t.__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1
    print(f"\n{'=' * 60}")
    print(f"V4.6.1 cooldown + multi_account 单元测试: {passed} passed, {failed} failed")
    print(f"{'=' * 60}")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()