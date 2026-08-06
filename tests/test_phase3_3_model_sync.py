"""Phase 3.3 (2026-07-29) — 自动入组/出组 单元测试

覆盖：
1. should_auto_join 高置信度 + tier + family
2. should_auto_leave deprecated + 0.9
3. process_model_item 高置信度新建 registry + 自动入组
4. process_model_item 中置信度写 log 不入 registry
5. process_model_item 低置信度被拒绝
6. process_model_item 出组逻辑
7. process_model_item 重复入组幂等
8. process_promotion raw_data 非 model_metadata 返空
9. process_promotion raw_data 解析失败返空
10. process_promotion 完整端到端：multi-model raw_data

跑法：python tests/test_phase3_3_model_sync.py
"""

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ============================================================
# Mock config
# ============================================================
def _install_mock_config():
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


_install_mock_config()


# ============================================================
# In-memory DB mock for model_sync (覆盖 crud.* 的关键路径)
# ============================================================
def _install_mock_db():
    """替换 crud.* 和 async_session_factory, 用 in-memory dict 模拟 DB"""
    import db.crud as crud
    from intelligence import model_sync

    _state = {
        "registry": {},   # model_id -> dict
        "members": {},    # (group_id, model_id) -> dict
        "logs": [],       # list[dict]
        "groups": {       # group_id -> dict (同步 seed_model_groups.py 的 tier 语义：T0=旗舰, T4=微型)
            "tier:flagship": {"group_id": "tier:flagship", "tier": None,  "enabled": 1},  # 不限档，用 min_context 兑底
            "tier:strong":   {"group_id": "tier:strong",   "tier": "T1", "enabled": 1},
            "tier:balanced": {"group_id": "tier:balanced", "tier": "T2", "enabled": 1},
            "tier:light":    {"group_id": "tier:light",    "tier": "T3", "enabled": 1},
        },
    }

    async def get_model_registry(session, model_id):
        row = _state["registry"].get(model_id)
        return _RegistryShim(row) if row else None

    async def upsert_model_registry(session, model_id, family, tier="T2", **kwargs):
        if model_id in _state["registry"]:
            _state["registry"][model_id].update(kwargs)
        else:
            _state["registry"][model_id] = {
                "model_id": model_id, "family": family, "tier": tier, **kwargs,
            }
        return _RegistryShim(_state["registry"][model_id])

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

    async def log_capability_change(session, model_id, field_changed, old_value,
                                       new_value, source="ai_infer",
                                       confidence=None, approved=0):
        _state["logs"].append({
            "model_id": model_id, "field_changed": field_changed,
            "old_value": old_value, "new_value": new_value,
            "source": source, "confidence": confidence, "approved": approved,
        })

    # Patch crud
    crud.get_model_registry = get_model_registry
    crud.upsert_model_registry = upsert_model_registry
    crud.add_model_group_member = add_model_group_member
    crud.log_capability_change = log_capability_change

    # Patch model_sync._find_target_group (直接读内存 groups)
    from db.models import ModelGroup
    from sqlalchemy import select

    class _GroupShim:
        def __init__(self, data):
            self.group_id = data["group_id"]
            self.tier = data["tier"]
            self.enabled = data["enabled"]

    class _MockSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def commit(self):
            pass

    class _MockSessionCtx:
        async def __aenter__(self):
            return _MockSession()

        async def __aexit__(self, *a):
            return False

    async def _find_target_group(session, tier):
        for g in _state["groups"].values():
            if g["enabled"] and g["group_id"].startswith("tier:") and g["tier"] == tier:
                return g["group_id"]
        return None

    model_sync._find_target_group = _find_target_group

    def _factory():
        return _MockSessionCtx()

    model_sync.async_session_factory = _factory

    return _state


class _RegistryShim:
    def __init__(self, data):
        self._data = data
        self.model_id = data["model_id"]
        self.family = data["family"]
        self.tier = data["tier"]
        self.display_name = data.get("display_name")
        self.context_length = data.get("context_length")
        self.modality = data.get("modality")
        self.status = data.get("status", "active")
        self.notes = data.get("notes")
        self.is_open_source = data.get("is_open_source", 1)

    def __setattr__(self, key, value):
        if key in {"_data", "model_id", "family", "tier", "display_name",
                    "context_length", "modality", "status", "notes", "is_open_source"} \
                and hasattr(self, "_data"):
            if key != "_data":
                self._data[key] = value
        super().__setattr__(key, value)


# ============================================================
# Test 1: should_auto_join
# ============================================================
def test_should_auto_join():
    print("\n=== Test 1: should_auto_join ===")
    from intelligence.model_sync import should_auto_join
    from schemas.intel import ModelIntelItem

    ok = ModelIntelItem(
        model_id="m1", family="qwen", inferred_tier="T2",
        confidence=0.9, confidence_level="high",
    )
    assert should_auto_join(ok), "0.9 + T2 + family 应通过"

    no_tier = ModelIntelItem(
        model_id="m2", family="qwen", inferred_tier=None,
        confidence=0.9, confidence_level="high",
    )
    assert not should_auto_join(no_tier), "tier 缺失不应入组"

    no_family = ModelIntelItem(
        model_id="m3", family=None, inferred_tier="T2",
        confidence=0.9, confidence_level="high",
    )
    assert not should_auto_join(no_family), "family 缺失不应入组"

    low_conf = ModelIntelItem(
        model_id="m4", family="qwen", inferred_tier="T2",
        confidence=0.7, confidence_level="medium",
    )
    assert not should_auto_join(low_conf), "0.7 < 0.8 不应入组"
    print("  [OK] should_auto_join 4 子场景全过")


# ============================================================
# Test 2: should_auto_leave
# ============================================================
def test_should_auto_leave():
    print("\n=== Test 2: should_auto_leave ===")
    from intelligence.model_sync import should_auto_leave
    from schemas.intel import ModelIntelItem

    yes = ModelIntelItem(
        model_id="m1", family="qwen", inferred_tier="T2",
        confidence=0.95, status="deprecated", confidence_level="high",
    )
    assert should_auto_leave(yes), "deprecated + 0.95 应出组"

    borderline = ModelIntelItem(
        model_id="m2", family="qwen", inferred_tier="T2",
        confidence=0.9, status="deprecated", confidence_level="high",
    )
    assert should_auto_leave(borderline), "deprecated + 0.9 应出组"

    too_low = ModelIntelItem(
        model_id="m3", family="qwen", inferred_tier="T2",
        confidence=0.85, status="deprecated", confidence_level="high",
    )
    assert not should_auto_leave(too_low), "0.85 < 0.9 不应自动出组"

    active = ModelIntelItem(
        model_id="m4", family="qwen", inferred_tier="T2",
        confidence=0.95, status="active", confidence_level="high",
    )
    assert not should_auto_leave(active), "active 状态不应触发出组"
    print("  [OK] should_auto_leave 4 子场景全过")


# ============================================================
# Test 3: process_model_item 高置信度新建 + 自动入组
# ============================================================
def test_process_auto_join_new():
    print("\n=== Test 3: process_model_item 高置信度新建 ===")
    state = _install_mock_db()
    from intelligence.model_sync import process_model_item
    from schemas.intel import ModelIntelItem

    item = ModelIntelItem(
        model_id="Pro/deepseek-ai/DeepSeek-V3", family="deepseek",
        inferred_tier="T2", capabilities=["code", "reasoning"],
        context_length=64000, confidence=0.92, confidence_level="high",
    )

    async def _run():
        async with state and __import__("contextlib").asynccontextmanager(lambda: (yield))():
            pass
        return await process_model_item(_MockSessionShim(), item)

    # 简化: 直接调用, 用 stub session
    class _S:
        pass

    result = asyncio.run(process_model_item(_S(), item))
    assert result.action == "auto_joined", f"应 auto_joined, 实际 {result.action}"
    assert result.target_group == "tier:balanced", f"应 tier:balanced, 实际 {result.target_group}"
    assert "Pro/deepseek-ai/DeepSeek-V3" in state["registry"]
    assert ("tier:balanced", "Pro/deepseek-ai/DeepSeek-V3") in state["members"]
    # log: create + tier (永远不直接落库)
    field_changes = [l["field_changed"] for l in state["logs"]]
    assert "create" in field_changes
    assert "tier" in field_changes
    # tier log approved=0 (人工确认)
    tier_log = next(l for l in state["logs"] if l["field_changed"] == "tier")
    assert tier_log["approved"] == 0, "tier 永远人工确认"
    print(f"  [OK] 新建 registry + 入组 {result.target_group} + log create/tier")


class _MockSessionShim:
    pass


# ============================================================
# Test 4: process_model_item 中置信度
# ============================================================
def test_process_pending_review():
    print("\n=== Test 4: process_model_item 中置信度 ===")
    state = _install_mock_db()
    from intelligence.model_sync import process_model_item
    from schemas.intel import ModelIntelItem

    item = ModelIntelItem(
        model_id="m-medium", family="qwen", inferred_tier="T2",
        confidence=0.7, confidence_level="medium",
    )
    result = asyncio.run(process_model_item(_MockSessionShim(), item))
    assert result.action == "pending_review", f"应 pending_review, 实际 {result.action}"
    assert "m-medium" not in state["registry"], "中置信度不应写 registry"
    # 应有 1 条 metadata log
    field_changes = [l["field_changed"] for l in state["logs"]]
    assert "metadata" in field_changes
    print(f"  [OK] 中置信度 0.7 写 log 不入 registry")


# ============================================================
# Test 5: process_model_item 低置信度
# ============================================================
def test_process_rejected():
    print("\n=== Test 5: process_model_item 低置信度 ===")
    state = _install_mock_db()
    from intelligence.model_sync import process_model_item
    from schemas.intel import ModelIntelItem

    item = ModelIntelItem(
        model_id="m-low", family="qwen", inferred_tier=None,
        confidence=0.3, confidence_level="low",
    )
    result = asyncio.run(process_model_item(_MockSessionShim(), item))
    assert result.action == "rejected", f"应 rejected, 实际 {result.action}"
    assert "m-low" not in state["registry"]
    field_changes = [l["field_changed"] for l in state["logs"]]
    assert "metadata" in field_changes
    print(f"  [OK] 低置信度 0.3 拒绝, 写 log")


# ============================================================
# Test 6: process_model_item 出组
# ============================================================
def test_process_auto_leave():
    print("\n=== Test 6: process_model_item 自动出组 ===")
    state = _install_mock_db()
    from intelligence.model_sync import process_model_item
    from schemas.intel import ModelIntelItem

    # 先有 registry 行
    state["registry"]["old-model"] = {
        "model_id": "old-model", "family": "qwen", "tier": "T2", "status": "active",
    }

    item = ModelIntelItem(
        model_id="old-model", family="qwen", inferred_tier="T2",
        confidence=0.95, status="deprecated", confidence_level="high",
    )
    result = asyncio.run(process_model_item(_MockSessionShim(), item))
    assert result.action == "auto_left", f"应 auto_left, 实际 {result.action}"
    assert state["registry"]["old-model"]["status"] == "deprecated"
    # log: status 变更 approved=1 (auto_left 阈值 0.9, 自动生效)
    status_log = next(l for l in state["logs"] if l["field_changed"] == "status")
    assert status_log["approved"] == 1
    assert status_log["old_value"] == "active"
    assert status_log["new_value"] == "deprecated"
    print(f"  [OK] 自动出组 status=deprecated, approved=1")


# ============================================================
# Test 7: process_model_item 重复入组幂等
# ============================================================
def test_process_idempotent():
    print("\n=== Test 7: process_model_item 重复入组幂等 ===")
    state = _install_mock_db()
    from intelligence.model_sync import process_model_item
    from schemas.intel import ModelIntelItem

    item = ModelIntelItem(
        model_id="dup-model", family="qwen", inferred_tier="T2",
        confidence=0.9, confidence_level="high",
    )
    r1 = asyncio.run(process_model_item(_MockSessionShim(), item))
    r2 = asyncio.run(process_model_item(_MockSessionShim(), item))
    # 第一次入组, 第二次 add_model_group_member 返回 False (但 action 仍为 auto_joined)
    assert r1.action == "auto_joined"
    assert r2.action == "auto_joined"
    # registry 应只有 1 条
    assert "dup-model" in state["registry"]
    # member 应只有 1 条
    members_for_model = [
        m for (g, m), v in state["members"].items() if m == "dup-model"
    ]
    assert len(members_for_model) == 1, f"应 1 条 member, 实际 {len(members_for_model)}"
    print(f"  [OK] 重复调用幂等, members={len(members_for_model)}")


# ============================================================
# Test 8: process_promotion 非 model_metadata 返空
# ============================================================
def test_process_promotion_wrong_kind():
    print("\n=== Test 8: process_promotion 非 model_metadata 返空 ===")
    _install_mock_db()
    from intelligence.model_sync import process_promotion

    raw = json_dumps = '{"_intelligence_kind": "promotion", "title": "8折"}'
    result = asyncio.run(process_promotion(1, raw))
    assert result == []
    print("  [OK] promotion raw_data 不处理")


# ============================================================
# Test 9: process_promotion JSON 失败
# ============================================================
def test_process_promotion_bad_json():
    print("\n=== Test 9: process_promotion JSON 解析失败 ===")
    _install_mock_db()
    from intelligence.model_sync import process_promotion

    result = asyncio.run(process_promotion(1, "{bad json"))
    assert result == []
    result2 = asyncio.run(process_promotion(1, ""))
    assert result2 == []
    print("  [OK] 解析失败/空字符串返 []")


# ============================================================
# Test 10: process_promotion 端到端 multi-model
# ============================================================
def test_process_promotion_end_to_end():
    print("\n=== Test 10: process_promotion 端到端 ===")
    state = _install_mock_db()
    from intelligence.model_sync import process_promotion
    import json

    raw = json.dumps({
        "_intelligence_kind": "model_metadata",
        "models": [
            {
                "model_id": "Qwen/Qwen2.5-72B-Instruct", "family": "qwen",
                "inferred_tier": "T3", "capabilities": ["reasoning", "code"],
                "context_length": 32768, "confidence": 0.92,
            },
            {
                "model_id": "deepseek-v2.5", "family": "deepseek",
                "inferred_tier": "T2", "capabilities": ["code"],
                "context_length": 32768, "confidence": 0.7,  # 中置信度
            },
            {
                "model_id": "garbage-model", "family": "qwen",
                "inferred_tier": None, "capabilities": [],
                "context_length": None, "confidence": 0.3,  # 低
            },
        ],
    })

    results = asyncio.run(process_promotion(42, raw))
    assert len(results) == 3, f"应 3 个结果, 实际 {len(results)}"
    assert results[0].action == "auto_joined"
    # explicit_tier=T3 (轻量档) 路由到 tier:light (seed 里 light.tier=T3)
    assert results[0].target_group == "tier:light", f"应 tier:light, 实际 {results[0].target_group}"
    assert results[1].action == "pending_review"
    assert results[2].action == "rejected"
    print(f"  [OK] 3 model 各自正确处理:")
    for r in results:
        print(f"        {r.action:20s}  {r.model_id}  conf={r.confidence:.2f}")


# ============================================================
# Main
# ============================================================
def main():
    print("=" * 60)
    print("Phase 3.3 — 自动入组/出组 测试")
    print("=" * 60)

    test_should_auto_join()
    test_should_auto_leave()
    test_process_auto_join_new()
    test_process_pending_review()
    test_process_rejected()
    test_process_auto_leave()
    test_process_idempotent()
    test_process_promotion_wrong_kind()
    test_process_promotion_bad_json()
    test_process_promotion_end_to_end()

    print("\n" + "=" * 60)
    print("ALL 10 TESTS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    main()
