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

    # Phase 4 v4.1: model_sync.auto_left 路径中同步调 remove_model_group_members_by_model
    # 全局 mock 在 _install_mock_db 里, 避免每个 auto_left 测试重复 patch.
    async def remove_model_group_members_by_model(session, model_id, *,
                                                   reason="manual", removed_by="manual_approve"):
        affected = 0
        for m in list(_state["members"].values()):
            if m["model_id"] == model_id and m.get("is_active", 1) == 1:
                m["is_active"] = 0
                m["removed_at"] = __import__("datetime").datetime.now()
                m["removed_reason"] = reason
                m["removed_by"] = removed_by
                affected += 1
        return affected

    async def log_capability_change(session, model_id, field_changed, old_value,
                                       new_value, source="ai_infer",
                                       confidence=None, approved=0,
                                       provider=None):
        _state["logs"].append({
            "model_id": model_id, "field_changed": field_changed,
            "old_value": old_value, "new_value": new_value,
            "source": source, "confidence": confidence, "approved": approved,
            "provider": provider,
        })

    # V4.8.6: mock has_available_channel — 默认 True (有可用渠道, 保 auto-join 语义);
    # 2026-10-12 intel_auto 护栏后, auto_left 测试需在 _state["channel_supply"]
    # 显式置 False (渠道无供应) 才走原全自动出组路径.
    async def has_available_channel(session, model_id):
        supply = _state.setdefault("channel_supply", {})
        return supply.get(model_id, True)

    # Patch crud
    crud.get_model_registry = get_model_registry
    crud.upsert_model_registry = upsert_model_registry
    crud.add_model_group_member = add_model_group_member
    crud.remove_model_group_members_by_model = remove_model_group_members_by_model
    crud.log_capability_change = log_capability_change
    crud.has_available_channel = has_available_channel

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

        async def flush(self):
            pass

        # P1-3 fix: begin_nested 返回 savepoint context manager
        # 让 process_promotion 的 savepoint 路径能在 mock 下跑
        def begin_nested(self):
            class _Savepoint:
                async def __aenter__(self_inner):
                    return self_inner

                async def __aexit__(self_inner, *exc):
                    # 如果有异常, 自动 rollback savepoint
                    if exc and exc[0] is not None:
                        # 模拟 rollback 行为: 只回滚本 savepoint 修改
                        pass
                    return False  # 不吞异常, 让外层 catch
            return _Savepoint()

    class _MockSessionCtx:
        async def __aenter__(self):
            return _MockSession()

        async def __aexit__(self, *a):
            return False

    # P0-3 fix: 模型同步的 _find_target_group 现在只认 T0 硬编码路由到 "tier:flagship",
    # 其他 tier 走人工确认. mock 跟 real logic 同步。
    async def _find_target_group(session, tier):
        if tier == "T0":
            return "tier:flagship"
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
    # P0-3 fix: T2 不是 T0, _find_target_group 返 None → action=auto_joined 但不入 group。
    # 等 dashboard 批准 tier log 后由 E1 approve path 负责碓入正确 group。
    assert result.action == "auto_joined", f"应 auto_joined, 实际 {result.action}"
    assert result.target_group is None, (
        f"T2 不应硬路由到 tier:balanced, 实际 {result.target_group}"
    )
    assert "Pro/deepseek-ai/DeepSeek-V3" in state["registry"]
    # 不应加入任何 tier: 组
    members_for_model = [
        k for k in state["members"].keys() if k[1] == "Pro/deepseek-ai/DeepSeek-V3"
    ]
    assert members_for_model == [], (
        f"T2 不应入组, 实际 members={members_for_model}"
    )
    # log: create + tier (永远不直接落库)
    field_changes = [l["field_changed"] for l in state["logs"]]
    assert "create" in field_changes
    assert "tier" in field_changes
    # tier log approved=0 (人工确认)
    tier_log = next(l for l in state["logs"] if l["field_changed"] == "tier")
    assert tier_log["approved"] == 0, "tier 永远人工确认"
    assert tier_log["new_value"] == "T2"
    print(f"  [OK] T2 新建 registry + 不入 group + 写 tier log (等人工确认)")


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
    # 预存 group_member (该 model 在 tier:balanced 组中, P1-2 schema)
    state["members"][("tier:balanced", "old-model")] = {
        "group_id": "tier:balanced", "model_id": "old-model",
        "priority": 100, "weight": 1.0, "added_by": "intel_auto",
        "is_active": 1, "removed_at": None, "removed_reason": None, "removed_by": None,
    }
    # 2026-10-12 护栏: 渠道无供应才走原全自动出组路径
    state.setdefault("channel_supply", {})["old-model"] = False

    # Phase 4 v4.1: model_sync.auto_left 路径中同步调 remove_model_group_members_by_model
    # (之前只在 P1-2 cleanup 脚本里才调). _install_mock_db() 已装全局 mock, 这里只 trace 调用.
    removed_calls = []

    import db.crud as crud
    _orig_remove = crud.remove_model_group_members_by_model

    async def _traced_remove(session, model_id, *, reason="manual", removed_by="manual_approve"):
        removed_calls.append({"model_id": model_id, "reason": reason, "removed_by": removed_by})
        return await _orig_remove(session, model_id, reason=reason, removed_by=removed_by)

    crud.remove_model_group_members_by_model = _traced_remove

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

    # Phase 4 v4.1 新增验证: member 一步软删 (不依赖 cleanup 任务)
    member = state["members"][("tier:balanced", "old-model")]
    assert member["is_active"] == 0, (
        f"v4.1 后 model_sync 应同步软删 member, 实际 is_active={member['is_active']}"
    )
    assert member["removed_reason"] == "deprecated+conf>=0.9"
    assert member["removed_by"] == "intel_auto"
    assert member["removed_at"] is not None

    # 验证 remove_model_group_members_by_model 被调用了, 且参数正确
    assert len(removed_calls) == 1, (
        f"应调用 1 次 remove_model_group_members_by_model, 实际 {len(removed_calls)}"
    )
    assert removed_calls[0]["model_id"] == "old-model"
    assert removed_calls[0]["reason"] == "deprecated+conf>=0.9"
    assert removed_calls[0]["removed_by"] == "intel_auto"

    print(f"  [OK] 自动出组 status=deprecated + 一步软删 member + reason=deprecated+conf>=0.9")


# ============================================================
# Test 7: process_model_item 重复入组幂等
# ============================================================
def test_process_idempotent():
    print("\n=== Test 7: process_model_item 重复入组幂等 ===")
    state = _install_mock_db()
    from intelligence.model_sync import process_model_item
    from schemas.intel import ModelIntelItem

    # P0-3 fix: 用 T0 才能走到自动入组, 才能验证幂等 "写 1 条 member"。
    # T1/T2/T3/T4 不入组, 幂等性在 tier log 重复上体现 (Test 14) 而不是 member 上。
    item = ModelIntelItem(
        model_id="dup-model", family="qwen", inferred_tier="T0",
        confidence=0.9, confidence_level="high",
    )
    r1 = asyncio.run(process_model_item(_MockSessionShim(), item))
    r2 = asyncio.run(process_model_item(_MockSessionShim(), item))
    # 第一次入组, 第二次 add_model_group_member 返回 False (但 action 仍为 auto_joined)
    assert r1.action == "auto_joined"
    assert r2.action == "auto_joined"
    # registry 应只有 1 条
    assert "dup-model" in state["registry"]
    # member 应只有 1 条 (进入 tier:flagship)
    members_for_model = [
        m for (g, m), v in state["members"].items() if m == "dup-model"
    ]
    assert len(members_for_model) == 1, f"应 1 条 member, 实际 {len(members_for_model)}"
    assert ("tier:flagship", "dup-model") in state["members"], (
        "T0 旗舰应入 tier:flagship"
    )
    print(f"  [OK] T0 重复调用幂等, members={len(members_for_model)} (tier:flagship)")


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

    # P0-3 fix: 第一个 model 改为 T0 (旗舰) - 走 flagship 路由,
    # 第二个 model 为 T2 (均衡) - 不入组, “queried tier log 后人工确认”。
    raw = json.dumps({
        "_intelligence_kind": "model_metadata",
        "models": [
            {
                "model_id": "Pro/Qwen/Qwen3-235B", "family": "qwen",
                "inferred_tier": "T0", "capabilities": ["reasoning", "code"],
                "context_length": 128000, "confidence": 0.95,
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
    # T0 旗舰 → 路由到 tier:flagship
    assert results[0].action == "auto_joined"
    assert results[0].target_group == "tier:flagship", (
        f"T0 旗舰应路由到 tier:flagship, 实际 {results[0].target_group}"
    )
    assert results[1].action == "pending_review"
    assert results[2].action == "rejected"
    # 不应有任何 T3/tier:light 路由
    light_routes = [r for r in results if r.target_group == "tier:light"]
    assert light_routes == [], "T1/T2/T3/T4 不应被碓路由"
    print(f"  [OK] 3 model 各自正确处理:")
    for r in results:
        print(f"        {r.action:20s}  {r.model_id}  conf={r.confidence:.2f}  group={r.target_group}")


# ============================================================
# Test 11 (P0-4 fix): 出组分支无条件 return, 已 deprecated 不准 fallthrough 重新入组
# ============================================================
def test_process_auto_leave_already_deprecated_no_fallthrough():
    print("\n=== Test 11: P0-4 出组分支已 deprecated 无 fallthrough ===")
    state = _install_mock_db()
    from intelligence.model_sync import process_model_item
    from schemas.intel import ModelIntelItem

    # 预存一个已 deprecated 的 model
    state["registry"]["old-deprecated"] = {
        "model_id": "old-deprecated", "family": "qwen",
        "tier": "T1", "status": "deprecated", "context_length": 32000,
        "modality": "text",
    }
    # 2026-10-12 护栏: 渠道无供应才走原全自动出组路径
    state.setdefault("channel_supply", {})["old-deprecated"] = False

    item = ModelIntelItem(
        model_id="old-deprecated", family="qwen", inferred_tier="T1",
        confidence=0.95, status="deprecated", confidence_level="high",
    )
    result = asyncio.run(process_model_item(_MockSessionShim(), item))
    # 应返回 auto_left (不会走到入组逻辑)
    assert result.action == "auto_left", f"应 auto_left, 实际 {result.action}"
    # 不能因为 “重复扫” 而被重新加入 tier:strong (那是入组逻辑的产物)
    members_for_model = [
        k for k in state["members"].keys() if k[1] == "old-deprecated"
    ]
    assert members_for_model == [], (
        f"已 deprecated 的 model 不应被入组, 实际 members={members_for_model}"
    )
    # status log 不应重复 (因为没状态变化)
    status_logs = [l for l in state["logs"] if l["field_changed"] == "status"]
    assert len(status_logs) == 0, (
        f"已 deprecated 重复扫不应再写 status log, 实际 {len(status_logs)} 条"
    )
    print("  [OK] 已 deprecated 重复扫不 fallthrough, 不入组, 不重写 status log")


# ============================================================
# Test 12 (P0-4 fix): 出组但 registry 里不存在 → 也不能 fallthrough
# ============================================================
def test_process_auto_leave_registry_absent():
    print("\n=== Test 12: P0-4 出组但 registry 不存在 ===")
    state = _install_mock_db()
    from intelligence.model_sync import process_model_item
    from schemas.intel import ModelIntelItem

    # 2026-10-12 护栏: 渠道无供应才走原全自动出组路径
    state.setdefault("channel_supply", {})["never-existed"] = False

    item = ModelIntelItem(
        model_id="never-existed", family="qwen", inferred_tier="T1",
        confidence=0.95, status="deprecated", confidence_level="high",
    )
    result = asyncio.run(process_model_item(_MockSessionShim(), item))
    assert result.action == "auto_left", f"应 auto_left, 实际 {result.action}"
    # 不能走入组逻辑 → members 为空
    assert state["members"] == {}, (
        f"registry 不存在的 deprecated model 不应入组, 实际 members={state['members']}"
    )
    print("  [OK] registry 缺行的 deprecated model 不被意外入组")


# ============================================================
# Test 13 (P0-5 fix): context_length / modality 变更走 log, 不 mutate
# ============================================================
def test_process_context_length_modality_audit_log():
    print("\n=== Test 13: P0-5 context_length / modality 走 log ===")
    state = _install_mock_db()
    from intelligence.model_sync import process_model_item
    from schemas.intel import ModelIntelItem

    # 预存一个 model
    state["registry"]["ctx-test"] = {
        "model_id": "ctx-test", "family": "qwen",
        "tier": "T1", "status": "active",
        "context_length": 8192, "modality": "text",
    }

    item = ModelIntelItem(
        model_id="ctx-test", family="qwen", inferred_tier="T1",
        context_length=32768, modality="multimodal",
        confidence=0.9, confidence_level="high",
    )
    asyncio.run(process_model_item(_MockSessionShim(), item))

    # registry 中 context_length / modality 不应被直接改写
    reg = state["registry"]["ctx-test"]
    assert reg["context_length"] == 8192, (
        f"context_length 不应被 mutate, 实际 {reg['context_length']}"
    )
    assert reg["modality"] == "text", (
        f"modality 不应被 mutate, 实际 {reg['modality']}"
    )
    # 应该有 context_length + modality 两条变更 log (approved=0)
    ctx_logs = [l for l in state["logs"] if l["field_changed"] == "context_length"]
    mod_logs = [l for l in state["logs"] if l["field_changed"] == "modality"]
    assert len(ctx_logs) == 1 and ctx_logs[0]["approved"] == 0
    assert ctx_logs[0]["old_value"] == 8192 and ctx_logs[0]["new_value"] == 32768
    assert len(mod_logs) == 1 and mod_logs[0]["approved"] == 0
    assert mod_logs[0]["old_value"] == "text" and mod_logs[0]["new_value"] == "multimodal"
    print("  [OK] context_length/modality 走 log, 未直接 mutate registry")


# ============================================================
# Test 14 (P1-4 fix): 重复入组 tier 未变 → 不重写 tier log
# ============================================================
def test_process_repeat_join_no_tier_log_dedup():
    print("\n=== Test 14: P1-4 重复入组 tier 不变不写 tier log ===")
    state = _install_mock_db()
    from intelligence.model_sync import process_model_item
    from schemas.intel import ModelIntelItem

    # 第一次入组: tier=T2, 新建 registry
    item = ModelIntelItem(
        model_id="dup-tier", family="qwen", inferred_tier="T2",
        confidence=0.9, confidence_level="high",
    )
    asyncio.run(process_model_item(_MockSessionShim(), item))
    tier_logs_1 = [l for l in state["logs"] if l["field_changed"] == "tier"]
    assert len(tier_logs_1) == 1, f"首次应 1 条 tier log, 实际 {len(tier_logs_1)}"

    # 第二次入组: 同一 model_id, tier=T2 (未变), 应不重复写 tier log
    asyncio.run(process_model_item(_MockSessionShim(), item))
    tier_logs_2 = [l for l in state["logs"] if l["field_changed"] == "tier"]
    assert len(tier_logs_2) == 1, (
        f"tier 未变不应重写 log, 实际 {len(tier_logs_2)} 条"
    )
    print("  [OK] 重复入组 tier 不变只写 1 条 tier log")

    # 第三次: tier 变化 T2 → T3 → 应再写一条
    item2 = ModelIntelItem(
        model_id="dup-tier", family="qwen", inferred_tier="T3",
        confidence=0.9, confidence_level="high",
    )
    asyncio.run(process_model_item(_MockSessionShim(), item2))
    tier_logs_3 = [l for l in state["logs"] if l["field_changed"] == "tier"]
    assert len(tier_logs_3) == 2, (
        f"tier 变化应再写 1 条, 累计应 2 条, 实际 {len(tier_logs_3)}"
    )
    print(f"  [OK] tier 变化时再多写 1 条 log, 累计 {len(tier_logs_3)} 条")


# ============================================================
# Test 15 (P0-3 fix): T0 (旗舰) 硬编码路由到 tier:flagship
# ============================================================
def test_process_t0_routes_to_flagship():
    print("\n=== Test 15: P0-3 T0 旗舰 → tier:flagship ===")
    state = _install_mock_db()
    from intelligence.model_sync import process_model_item
    from schemas.intel import ModelIntelItem

    # 235B 旗舰 model
    item = ModelIntelItem(
        model_id="Pro/Qwen/Qwen3-235B-Instruct", family="qwen",
        inferred_tier="T0", capabilities=["reasoning", "code"],
        context_length=128000, confidence=0.95, confidence_level="high",
    )
    result = asyncio.run(process_model_item(_MockSessionShim(), item))
    # T0 唯一能免占路由的 tier
    assert result.action == "auto_joined"
    assert result.target_group == "tier:flagship", (
        f"T0 应路由到 tier:flagship, 实际 {result.target_group}"
    )
    # 确实写入 member
    assert ("tier:flagship", "Pro/Qwen/Qwen3-235B-Instruct") in state["members"], (
        "T0 旗舰应被加入 tier:flagship 组"
    )
    # 其他 tier: 组不应被碰到
    other_groups = [
        k for k in state["members"].keys()
        if k[0] in {"tier:strong", "tier:balanced", "tier:light"}
    ]
    assert other_groups == [], f"T0 只应入 flagship, 实际入 {other_groups}"
    print(f"  [OK] T0 旗舰 → tier:flagship, 不碰其他 tier: 组")


# ============================================================
# Test 16 (P0-3 fix): T1/T2/T3/T4 不再免占路由, action=auto_joined 但 target_group=None
# ============================================================
def test_process_non_t0_no_auto_route():
    print("\n=== Test 16: P0-3 T1/T2/T3/T4 不自动入 tier: 组 ===")
    state = _install_mock_db()
    from intelligence.model_sync import process_model_item
    from schemas.intel import ModelIntelItem

    for tier in ("T1", "T2", "T3", "T4"):
        state = _install_mock_db()  # 每个 tier 重置 state
        item = ModelIntelItem(
            model_id=f"model-{tier}", family="qwen",
            inferred_tier=tier, capabilities=["code"],
            confidence=0.9, confidence_level="high",
        )
        result = asyncio.run(process_model_item(_MockSessionShim(), item))
        # action 仍是 auto_joined (高置信度 + 有 tier + 有 family)
        assert result.action == "auto_joined", (
            f"{tier} 应 auto_joined (高置信度), 实际 {result.action}"
        )
        # 但 target_group 必须是 None (免占路由)
        assert result.target_group is None, (
            f"{tier} 不应自动路由, 实际 {result.target_group}"
        )
        # 任何 tier: 组都不应被加入
        tier_members = [
            k for k in state["members"].keys()
            if k[0].startswith("tier:") and k[1] == f"model-{tier}"
        ]
        assert tier_members == [], (
            f"{tier} 不应入组, 实际 members={tier_members}"
        )
        # 但 tier log 必须写 (dashboard 批准后会负责入组)
        tier_logs = [l for l in state["logs"] if l["field_changed"] == "tier"]
        assert len(tier_logs) == 1, (
            f"{tier} 应写 1 条 tier log (等人工确认), 实际 {len(tier_logs)}"
        )
        assert tier_logs[0]["new_value"] == tier
        assert tier_logs[0]["approved"] == 0
    print(f"  [OK] T1/T2/T3/T4 均不动加 tier: 组, 只写 tier log 等待人工")


# ============================================================
# Test 17 (P1-3 fix): process_promotion 单条 model 抛错不影响其他
# ============================================================
def test_process_promotion_single_failure_isolated():
    print("\n=== Test 17: P1-3 process_promotion 单条失败不影响其他 ===")
    state = _install_mock_db()
    from intelligence.model_sync import process_promotion, process_model_item
    from schemas.intel import ModelIntelItem
    import db.crud as crud

    # 在 state["registry"] 预置 model_3 的存在, 然后通过 patch 让 process_model_item
    # 在 model_3 时抛错 (测 savepoint 隔离 + 后续 model 仍 continue).
    _orig_process_model_item = process_model_item

    call_count = {"n": 0}

    async def _flaky_model_3(session, item):
        call_count["n"] += 1
        if item.model_id == "model-3":
            # 模拟 SQLAlchemy 异常 (IntegrityError / OperationalError 等)
            raise RuntimeError("simulated DB failure for model-3")
        return await _orig_process_model_item(session, item)

    # 替换 process_model_item 在 model_sync 模块内的引用
    import intelligence.model_sync as model_sync
    _orig_in_module = model_sync.process_model_item
    model_sync.process_model_item = _flaky_model_3

    try:
        # 5 个 model, 第 3 个会抛错
        raw_data = json.dumps({
            "_intelligence_kind": "model_metadata",
            "models": [
                {"model_id": "model-1", "family": "qwen", "inferred_tier": "T0",
                 "confidence": 0.95, "confidence_level": "high"},
                {"model_id": "model-2", "family": "qwen", "inferred_tier": "T0",
                 "confidence": 0.95, "confidence_level": "high"},
                {"model_id": "model-3", "family": "qwen", "inferred_tier": "T0",
                 "confidence": 0.95, "confidence_level": "high"},  # 这条会失败
                {"model_id": "model-4", "family": "qwen", "inferred_tier": "T0",
                 "confidence": 0.95, "confidence_level": "high"},
                {"model_id": "model-5", "family": "qwen", "inferred_tier": "T0",
                 "confidence": 0.95, "confidence_level": "high"},
            ],
        })

        results = asyncio.run(process_promotion(1, raw_data))
    finally:
        model_sync.process_model_item = _orig_in_module

    # P1-3 验证: 5 次调用, 第 3 次失败, 4 个 SyncResult 返回
    assert call_count["n"] == 5, f"应调用 5 次, 实际 {call_count['n']}"
    assert len(results) == 4, f"应返 4 条 (失败 1 条 skip), 实际 {len(results)}"

    # 验证成功 4 条都已写入 registry
    success_ids = [r.model_id for r in results]
    assert success_ids == ["model-1", "model-2", "model-4", "model-5"], \
        f"成功顺序应为 1,2,4,5, 实际 {success_ids}"

    # 失败的 model-3 不在 registry
    for mid in ["model-1", "model-2", "model-4", "model-5"]:
        assert mid in state["registry"], f"model {mid} 应在 registry"
    assert "model-3" not in state["registry"], \
        "失败的 model-3 不应在 registry"

    # 4 条都加入 tier:flagship (T0 硬编码路由)
    for mid in ["model-1", "model-2", "model-4", "model-5"]:
        assert ("tier:flagship", mid) in state["members"], \
            f"model {mid} 应入 tier:flagship"
    assert ("tier:flagship", "model-3") not in state["members"], \
        "失败的 model-3 不应入 group"
    print("  [OK] 单条失败 savepoint rollback, 后续 4 条正常处理")


# ============================================================
# Test 18 (P1-3 fix): Pydantic 失败已在 sync 阶段 skip, 不进入 savepoint
# ============================================================
def test_process_promotion_pydantic_skip_before_savepoint():
    print("\n=== Test 18: P1-3 Pydantic 验证失败 sync 阶段 skip ===")
    state = _install_mock_db()
    from intelligence.model_sync import process_promotion

    # confidence 是 string (非 float) -> Pydantic 验证失败 (sync 阶段 skip)
    raw_data = json.dumps({
        "_intelligence_kind": "model_metadata",
        "models": [
            {"model_id": "good-1", "family": "qwen", "inferred_tier": "T0",
             "confidence": 0.9, "confidence_level": "high"},
            {"model_id": "bad-1", "family": "qwen",
             "confidence": "not_a_number",
             "confidence_level": "high"},  # type error -> Pydantic fail
            {"model_id": "good-2", "family": "qwen", "inferred_tier": "T0",
             "confidence": 0.9, "confidence_level": "high"},
        ],
    })

    results = asyncio.run(process_promotion(1, raw_data))

    # 2 条 good, 1 条 bad pydantic-skipped
    assert len(results) == 2, f"应 2 条 (1 条 pydantic skip), 实际 {len(results)}"
    assert [r.model_id for r in results] == ["good-1", "good-2"], \
        f"顺序应为 good-1, good-2, 实际 {[r.model_id for r in results]}"
    assert "bad-1" not in state["registry"]
    assert "good-1" in state["registry"]
    assert "good-2" in state["registry"]
    print("  [OK] Pydantic 失败 sync skip, 后续 model 正常处理")


# ============================================================
# Test 19 (P1-3 fix): 全部成功 (控制对比)
# ============================================================
def test_process_promotion_all_success_baseline():
    print("\n=== Test 19: P1-3 全部成功 baseline (控制对比) ===")
    state = _install_mock_db()
    from intelligence.model_sync import process_promotion

    raw_data = json.dumps({
        "_intelligence_kind": "model_metadata",
        "models": [
            {"model_id": f"m{i}", "family": "qwen", "inferred_tier": "T0",
             "confidence": 0.9, "confidence_level": "high"}
            for i in range(5)
        ],
    })

    results = asyncio.run(process_promotion(1, raw_data))

    assert len(results) == 5
    assert len(state["registry"]) == 5, f"5 条全入 registry, 实际 {len(state['registry'])}"
    print("  [OK] 全部 5 条成功, savepoint 不影响 happy path")


# 引入 json (Test 17 用)
import json


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
    # P0-4 / P0-5 / P1-4 新增用例
    test_process_auto_leave_already_deprecated_no_fallthrough()
    test_process_auto_leave_registry_absent()
    test_process_context_length_modality_audit_log()
    test_process_repeat_join_no_tier_log_dedup()
    # P0-3 新增用例
    test_process_t0_routes_to_flagship()
    test_process_non_t0_no_auto_route()
    # P1-3 新增用例
    test_process_promotion_single_failure_isolated()
    test_process_promotion_pydantic_skip_before_savepoint()
    test_process_promotion_all_success_baseline()

    print("\n" + "=" * 60)
    print("ALL 19 TESTS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    main()
