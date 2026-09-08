"""Phase 4 E1 (2026-07-29) — Dashboard 待确认队列单元测试

覆盖 crud 三个函数:
1. list_pending_review 默认 approved=0 + source='ai_infer'
2. approve_capability_change 把 log.approved=1, 若 field_changed='tier' 则同步 registry.tier
3. reject_capability_change 把 log.approved=-1 (2026-09-09 三态: 0=待审/1=批准/-1=拒绝), 不动 registry

跑法: python tests/test_phase4_e1_pending_review.py
"""

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ============================================================
# Mock config + in-memory DB
# ============================================================
def _install_mock_env():
    import types

    class _MockSettings:
        openai_api_key = "sk-test"
        openai_base_url = "https://mock.local/v1"
        parse_model = "gpt-4o-mini"
        parse_confidence_threshold = 0.6
        crawl_enabled = True
        aimc_master_key = "test-key"
        route_refresh_interval = 600
        db_url = "sqlite+aiosqlite:///:memory:"
        db_path = "./data/aimc.db"

    sys.modules.setdefault("config", types.ModuleType("config"))
    sys.modules["config"].settings = _MockSettings()


_install_mock_env()


def _patch_crud_with_in_memory_db():
    """替换 crud.list_pending_review / approve_capability_change / reject_capability_change

    用内存字典模拟 model_capabilities_log + model_registry。
    """
    import db.crud as crud

    _state = {
        "registry": {
            "test-model-1": {"model_id": "test-model-1", "family": "qwen", "tier": "T2", "status": "active"},
            "test-model-2": {"model_id": "test-model-2", "family": "deepseek", "tier": "T3", "status": "active"},
        },
        "logs": {},  # log_id -> dict
        "next_log_id": 1,
        # P1-1: 模型组状态
        "groups": {
            "tier:flagship": {"group_id": "tier:flagship", "tier": None, "enabled": 1},
            "tier:strong":   {"group_id": "tier:strong",   "tier": "T1", "enabled": 1},
            "tier:balanced": {"group_id": "tier:balanced", "tier": "T2", "enabled": 1},
            "tier:light":    {"group_id": "tier:light",    "tier": "T3", "enabled": 1},
        },
        "members": {},  # (group_id, model_id) -> dict
    }

    def _add_log(model_id, field_changed, old_value, new_value,
                 source="ai_infer", confidence=0.8, approved=0):
        log_id = _state["next_log_id"]
        _state["next_log_id"] += 1
        _state["logs"][log_id] = {
            "id": log_id, "model_id": model_id, "field_changed": field_changed,
            "old_value": old_value, "new_value": new_value,
            "source": source, "confidence": confidence, "approved": approved,
        }
        return log_id

    class _LogShim:
        def __init__(self, data):
            self._data = data
            self.id = data["id"]
            self.model_id = data["model_id"]
            self.field_changed = data["field_changed"]
            self.old_value = data["old_value"]
            self.new_value = data["new_value"]
            self.source = data["source"]
            self.confidence = data["confidence"]
            self.approved = data["approved"]

        def __setattr__(self, key, value):
            if key == "approved":
                self._data["approved"] = value
            super().__setattr__(key, value)

    class _RegistryShim:
        def __init__(self, data):
            self._data = data
            self.model_id = data["model_id"]
            self.tier = data.get("tier")
            self.status = data.get("status", "active")
            self.family = data.get("family")

        def __setattr__(self, key, value):
            if key in {"_data", "model_id", "tier", "status", "family"} and hasattr(self, "_data"):
                if key != "_data":
                    self._data[key] = value
            super().__setattr__(key, value)

    # 预填 5 条 log 供测试
    _add_log("test-model-1", "tier", "T2", "T3", confidence=0.85, approved=0)
    _add_log("test-model-2", "tier", "T3", "T4", confidence=0.92, approved=0)
    _add_log("test-model-1", "create", None, "{\"model_id\":\"test-model-1\"}", confidence=0.85, approved=0)
    _add_log("test-model-2", "capabilities", "[\"code\"]", "[\"code\",\"reasoning\"]", confidence=0.75, approved=0)
    _add_log("test-model-1", "tier", "T2", "T1", source="manual", approved=1)  # 已确认 + manual → 排除
    _add_log("test-model-2", "tier", "T3", "T1", confidence=0.9, approved=1)  # 已确认 → 排除

    async def list_pending_review(session, limit=100):
        items = [
            v for v in _state["logs"].values()
            if v["approved"] == 0 and v["source"] == "ai_infer"
        ]
        items.sort(key=lambda x: -x["id"])  # 倒序
        return [_LogShim(item) for item in items[:limit]]

    async def approve_capability_change(session, log_id):
        log = _state["logs"].get(log_id)
        if not log:
            return None
        if log["approved"] == 1:
            return _LogShim(log)
        log["approved"] = 1
        if log["field_changed"] == "tier" and log["new_value"]:
            reg = _state["registry"].get(log["model_id"])
            if reg:
                # P1-7 fix: 先捕获旧值, 再 mutate, 最后 logger 顺序
                old_tier = reg["tier"]
                reg["tier"] = log["new_value"]
                # P1-1 fix: tier 迁移, 从旧组踢出 + 加入新组
                removed = await remove_model_group_members_by_model(session, log["model_id"])
                new_group = await find_target_group_for_tier(session, log["new_value"])
                if new_group:
                    await add_model_group_member(
                        session, new_group, log["model_id"],
                        priority=100, weight=1.0, added_by="manual_approve",
                    )
        elif log["field_changed"] == "status" and log["new_value"] == "deprecated":
            reg = _state["registry"].get(log["model_id"])
            if reg:
                reg["status"] = "deprecated"
        return _LogShim(log)

    async def reject_capability_change(session, log_id):
        log = _state["logs"].get(log_id)
        if not log:
            return None
        if log["approved"] != 0:
            return _LogShim(log)
        log["approved"] = -1
        # 拒绝: 不动 registry
        return _LogShim(log)

    # P1-1 fix: 迁移用 td crud 辅助
    async def remove_model_group_members_by_model(session, model_id):
        keys_to_remove = [k for k in _state["members"] if k[1] == model_id]
        for k in keys_to_remove:
            del _state["members"][k]
        return len(keys_to_remove)

    async def find_target_group_for_tier(session, tier):
        if tier == "T0":
            return "tier:flagship"
        for g in _state["groups"].values():
            if g["enabled"] and g["group_id"].startswith("tier:") and g["tier"] == tier:
                return g["group_id"]
        return None

    async def add_model_group_member(session, group_id, model_id,
                                       priority=100, weight=1.0, added_by="manual"):
        key = (group_id, model_id)
        if key in _state["members"]:
            return False
        _state["members"][key] = {
            "group_id": group_id, "model_id": model_id,
            "priority": priority, "weight": weight, "added_by": added_by,
        }
        return True

    crud.list_pending_review = list_pending_review
    crud.approve_capability_change = approve_capability_change
    crud.reject_capability_change = reject_capability_change
    crud.remove_model_group_members_by_model = remove_model_group_members_by_model
    crud.find_target_group_for_tier = find_target_group_for_tier
    crud.add_model_group_member = add_model_group_member

    return _state


# ============================================================
# Test 1: list_pending_review 默认过滤
# ============================================================
def test_list_pending_review_filters():
    print("\n=== Test 1: list_pending_review 过滤 ===")
    state = _patch_crud_with_in_memory_db()
    from db import crud

    async def _run():
        class _S:
            pass

        return await crud.list_pending_review(_S(), limit=10)

    items = asyncio.run(_run())
    # 期望: 4 条 (2 条 tier 待确认 + 1 条 create + 1 条 capabilities)
    # 排除: manual source + 2 条 approved=1
    assert len(items) == 4, f"应 4 条, 实际 {len(items)}"
    # 倒序: 最新在最上
    field_set = {i.field_changed for i in items}
    assert "tier" in field_set
    assert "create" in field_set
    assert "capabilities" in field_set
    # 不应有 manual 的
    sources = {i.source for i in items}
    assert sources == {"ai_infer"}
    # 不应有 approved=1
    assert all(i.approved == 0 for i in items)
    print(f"  [OK] 过滤后剩 {len(items)} 条 (排除 manual={1} approved={2})")


# ============================================================
# Test 2: list_pending_review limit 截断
# ============================================================
def test_list_pending_review_limit():
    print("\n=== Test 2: list_pending_review limit ===")
    state = _patch_crud_with_in_memory_db()
    from db import crud

    async def _run():
        class _S:
            pass

        return await crud.list_pending_review(_S(), limit=2)

    items = asyncio.run(_run())
    assert len(items) == 2
    print(f"  [OK] limit=2 截断到 {len(items)} 条")


# ============================================================
# Test 3: approve tier → registry.tier 同步
# ============================================================
def test_approve_tier_updates_registry():
    print("\n=== Test 3: approve tier → registry.tier 同步 ===")
    state = _patch_crud_with_in_memory_db()
    from db import crud

    # log #1: test-model-1 tier T2 → T3
    assert state["registry"]["test-model-1"]["tier"] == "T2"
    async def _run():
        class _S:
            pass

        return await crud.approve_capability_change(_S(), 1)

    log = asyncio.run(_run())
    assert log.approved == 1
    assert state["registry"]["test-model-1"]["tier"] == "T3"
    print(f"  [OK] 批准 tier 同步: T2 → T3")


# ============================================================
# Test 4: approve 非 tier 字段不动 registry
# ============================================================
def test_approve_non_tier_no_op():
    print("\n=== Test 4: approve 非 tier 字段不动 registry ===")
    state = _patch_crud_with_in_memory_db()
    from db import crud

    # log #3: create → 保留
    # log #4: capabilities → 保留
    original_tier_1 = state["registry"]["test-model-1"]["tier"]
    original_tier_2 = state["registry"]["test-model-2"]["tier"]

    async def _run(log_id):
        class _S:
            pass

        return await crud.approve_capability_change(_S(), log_id)

    log3 = asyncio.run(_run(3))
    log4 = asyncio.run(_run(4))
    assert log3.approved == 1
    assert log4.approved == 1
    # tier 不变
    assert state["registry"]["test-model-1"]["tier"] == original_tier_1
    assert state["registry"]["test-model-2"]["tier"] == original_tier_2
    print("  [OK] approve capabilities/create 不动 registry.tier")


# ============================================================
# Test 5: approve 幂等
# ============================================================
def test_approve_idempotent():
    print("\n=== Test 5: approve 幂等 ===")
    state = _patch_crud_with_in_memory_db()
    from db import crud

    async def _run(log_id):
        class _S:
            pass

        return await crud.approve_capability_change(_S(), log_id)

    log1 = asyncio.run(_run(1))
    log1_again = asyncio.run(_run(1))
    assert log1.approved == 1
    assert log1_again.approved == 1
    # tier 只更新一次 (仍是 T3)
    assert state["registry"]["test-model-1"]["tier"] == "T3"
    print("  [OK] 重复批准幂等")


# ============================================================
# Test 6: approve 不存在 log
# ============================================================
def test_approve_missing():
    print("\n=== Test 6: approve 不存在的 log ===")
    state = _patch_crud_with_in_memory_db()
    from db import crud

    async def _run():
        class _S:
            pass

        return await crud.approve_capability_change(_S(), 999)

    log = asyncio.run(_run())
    assert log is None
    print("  [OK] approve 999 → None")


# ============================================================
# Test 7: reject 不动 registry
# ============================================================
def test_reject_keeps_registry():
    print("\n=== Test 7: reject 不动 registry ===")
    state = _patch_crud_with_in_memory_db()
    from db import crud

    # log #2: test-model-2 tier T3 → T4, 应被拒绝
    original_tier = state["registry"]["test-model-2"]["tier"]
    assert original_tier == "T3"

    async def _run():
        class _S:
            pass

        return await crud.reject_capability_change(_S(), 2)

    log = asyncio.run(_run())
    assert log.approved == -1
    # tier 保持 T3
    assert state["registry"]["test-model-2"]["tier"] == "T3"
    print(f"  [OK] reject 保持 tier={state['registry']['test-model-2']['tier']}")


# ============================================================
# Test 8: reject 幂等
# ============================================================
def test_reject_idempotent():
    print("\n=== Test 8: reject 幂等 ===")
    state = _patch_crud_with_in_memory_db()
    from db import crud

    async def _run(log_id):
        class _S:
            pass

        return await crud.reject_capability_change(_S(), log_id)

    log = asyncio.run(_run(2))
    log_again = asyncio.run(_run(2))
    assert log.approved == -1
    assert log_again.approved == -1
    print("  [OK] reject 重复幂等")


# ============================================================
# Test 9: 完整流程: list → approve 一条
# ============================================================
def test_workflow_list_then_approve():
    print("\n=== Test 9: 完整流程 list → approve ===")
    state = _patch_crud_with_in_memory_db()
    from db import crud

    async def _list():
        class _S:
            pass

        return await crud.list_pending_review(_S(), limit=10)

    async def _approve(log_id):
        class _S:
            pass

        return await crud.approve_capability_change(_S(), log_id)

    items = asyncio.run(_list())
    assert len(items) == 4

    # 取第一个 (id 最大, 即 #4 capabilities)
    first = items[0]
    assert first.id == 4
    log = asyncio.run(_approve(first.id))
    assert log.approved == 1

    # 再 list, 应剩 3 条
    items2 = asyncio.run(_list())
    assert len(items2) == 3, f"应剩 3, 实际 {len(items2)}"
    print(f"  [OK] 批准 1 条后 list 剩 {len(items2)} 条")


# ============================================================
# Test 10 (P1-1 fix): approve tier T2→T0 → 从 balanced 转到 flagship
# ============================================================
def test_approve_tier_migrate_to_flagship():
    print("\n=== Test 10: P1-1 tier 迁移 T2 → T0, 入 flagship 踢出 balanced ===")
    state = _patch_crud_with_in_memory_db()
    from db import crud

    # 预填: test-model-1 已在 tier:balanced (模拟 register 状态)
    state["members"][("tier:balanced", "test-model-1")] = {
        "group_id": "tier:balanced", "model_id": "test-model-1",
        "priority": 100, "weight": 1.0, "added_by": "intel_auto",
    }
    # 新增一条 tier log: T2 → T0
    state["next_log_id"] = 100
    state["logs"][100] = {
        "id": 100, "model_id": "test-model-1", "field_changed": "tier",
        "old_value": "T2", "new_value": "T0",
        "source": "ai_infer", "confidence": 0.9, "approved": 0,
    }
    # 预填 registry tier 同步表示是 T2
    state["registry"]["test-model-1"]["tier"] = "T2"

    async def _run():
        class _S:
            pass

        return await crud.approve_capability_change(_S(), 100)

    log = asyncio.run(_run())
    assert log.approved == 1
    # registry.tier 同步为 T0
    assert state["registry"]["test-model-1"]["tier"] == "T0"
    # 应该从 tier:balanced 踢出
    assert ("tier:balanced", "test-model-1") not in state["members"], (
        "应从 tier:balanced 踢出, 实际仍存在"
    )
    # 应该加入 tier:flagship
    assert ("tier:flagship", "test-model-1") in state["members"], (
        "T0 应入 tier:flagship"
    )
    # 迁移一行加, 一行减, 净不应有 tier:balanced
    tier_members = [
        (g, m) for (g, m) in state["members"].keys() if m == "test-model-1"
    ]
    assert tier_members == [("tier:flagship", "test-model-1")], (
        f"test-model-1 只应入 flagship, 实际 {tier_members}"
    )
    print(f"  [OK] T2 → T0 迁移: balanced 踢出, flagship 加入")


# ============================================================
# Test 11 (P1-1 fix): approve tier T3→T1 → 从 light 转到 strong
# ============================================================
def test_approve_tier_migrate_balanced_to_strong():
    print("\n=== Test 11: P1-1 tier 迁移 T3 → T1, 从 light 转到 strong ===")
    state = _patch_crud_with_in_memory_db()
    from db import crud

    # 预填: test-model-2 已在 tier:light (T3)
    state["members"][("tier:light", "test-model-2")] = {
        "group_id": "tier:light", "model_id": "test-model-2",
        "priority": 100, "weight": 1.0, "added_by": "intel_auto",
    }
    state["next_log_id"] = 101
    state["logs"][101] = {
        "id": 101, "model_id": "test-model-2", "field_changed": "tier",
        "old_value": "T3", "new_value": "T1",
        "source": "ai_infer", "confidence": 0.9, "approved": 0,
    }
    state["registry"]["test-model-2"]["tier"] = "T3"

    async def _run():
        class _S:
            pass

        return await crud.approve_capability_change(_S(), 101)

    log = asyncio.run(_run())
    assert log.approved == 1
    assert state["registry"]["test-model-2"]["tier"] == "T1"
    # 从 light 踢出
    assert ("tier:light", "test-model-2") not in state["members"]
    # 加入 strong
    assert ("tier:strong", "test-model-2") in state["members"], (
        "T1 应入 tier:strong"
    )
    print(f"  [OK] T3 → T1 迁移: light 踢出, strong 加入")


# ============================================================
# Test 12 (P1-1 fix): approve tier 迁移幂等 — 重复批准不重复迁移
# ============================================================
def test_approve_tier_migrate_idempotent():
    print("\n=== Test 12: P1-1 tier 迁移幂等 ===")
    state = _patch_crud_with_in_memory_db()
    from db import crud

    state["members"][("tier:balanced", "test-model-1")] = {
        "group_id": "tier:balanced", "model_id": "test-model-1",
        "priority": 100, "weight": 1.0, "added_by": "intel_auto",
    }
    state["next_log_id"] = 102
    state["logs"][102] = {
        "id": 102, "model_id": "test-model-1", "field_changed": "tier",
        "old_value": "T2", "new_value": "T0",
        "source": "ai_infer", "confidence": 0.9, "approved": 0,
    }
    state["registry"]["test-model-1"]["tier"] = "T2"

    async def _run():
        class _S:
            pass

        await crud.approve_capability_change(_S(), 102)
        return await crud.approve_capability_change(_S(), 102)  # 重复

    asyncio.run(_run())
    # 重复调不应叠加迁移 (第二次打 approved=1 早返回)
    tier_members = [
        (g, m) for (g, m) in state["members"].keys() if m == "test-model-1"
    ]
    assert tier_members == [("tier:flagship", "test-model-1")], (
        f"幂等后只应 1 条 member, 实际 {tier_members}"
    )
    print(f"  [OK] 重复 approve 幂等, members={tier_members}")


# ============================================================
# Main
# ============================================================
def main():
    print("=" * 60)
    print("Phase 4 E1 — Dashboard 待确认队列 测试")
    print("=" * 60)

    test_list_pending_review_filters()
    test_list_pending_review_limit()
    test_approve_tier_updates_registry()
    test_approve_non_tier_no_op()
    test_approve_idempotent()
    test_approve_missing()
    test_reject_keeps_registry()
    test_reject_idempotent()
    test_workflow_list_then_approve()
    # P1-1 新增: tier 迁移
    test_approve_tier_migrate_to_flagship()
    test_approve_tier_migrate_balanced_to_strong()
    test_approve_tier_migrate_idempotent()

    print("\n" + "=" * 60)
    print("ALL 12 TESTS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    main()