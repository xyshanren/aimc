"""Phase 4 E3 (2026-07-29) — 跨源交叉验证单元测试

覆盖:
1. _normalize_field_value: tier 标准化 + capabilities 排序 + context_length 转 int
2. _calc_discrepancy_score: tier / capabilities / context_length / modality 各种打分
3. _group_models_by_id: 多 model + 多源聚合
4. detect_and_record: 端到端 (mock DB + Promotion + crud)
5. upsert_cross_source_discrepancy: 同 model+field+pending 覆盖 / 已 resolved 不覆盖
6. resolve_cross_source_discrepancy: resolved + ignored 路径 + 幂等
7. detect_and_record: 1 个 model 2 源一致 → 0 入库
8. detect_and_record: 1 个 model 2 源 tier 不同 → 1 入库
9. P0-7 fix: IntegrityError fallback (并发插入撞 UniqueConstraint → 重新 SELECT+UPDATE)

跑法: python tests/test_phase4_e3_cross_source.py
"""

import asyncio
import json
import sys
from pathlib import Path
from datetime import datetime
from typing import Optional

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ============================================================
# Mock config
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
        db_path = "./data/aimc.db"
        db_url = "sqlite+aiosqlite:///:memory:"
        alert_channel = "log"

    sys.modules.setdefault("config", types.ModuleType("config"))
    sys.modules["config"].settings = _MockSettings()


_install_mock_env()

# 保存真实的 upsert_cross_source_discrepancy 函数引用, 供 Test 11 (P0-7 fix)
# 验证 IntegrityError fallback 路径使用. _patch_db_with_in_memory() 会覆写
# 模块上的同名属性, 但我们保留 _ORIGINAL_UPSERT 指向原始函数.
from db import crud as _crud_module
_ORIGINAL_UPSERT = _crud_module.upsert_cross_source_discrepancy


# ============================================================
# Mock DB (promotions + cross_source_discrepancies)
# ============================================================
def _patch_db_with_in_memory():
    """替换 cross_source_validator + crud.* 的 DB 路径，用内存字典模拟。"""
    import db.crud as crud
    from intelligence import cross_source_validator as validator

    _state = {
        "promotions": [],   # list[Promotion-like dict]
        "discrepancies": {},  # discrepancy_id -> dict
        "next_discrepancy_id": 1,
    }

    def _add_promotion(provider: str, source_type: str, models: list, raw_data: Optional[str] = None):
        if raw_data is None:
            raw_data = json.dumps({
                "_intelligence_kind": "model_metadata",
                "_source_type": source_type,
                "models": models,
            }, ensure_ascii=False)
        _state["promotions"].append({
            "id": len(_state["promotions"]) + 1,
            "provider": provider,
            "status": "active",
            "raw_data": raw_data,
        })

    async def list_cross_source_discrepancies(session, status="pending", limit=100):
        items = [v for v in _state["discrepancies"].values() if v["status"] == status]
        items.sort(key=lambda x: (-x["discrepancy_score"], -x["id"]))
        return [_DiscShim(i) for i in items[:limit]]

    async def upsert_cross_source_discrepancy(session, model_id, field_changed,
                                                sources_json, discrepancy_score=0.0):
        for v in _state["discrepancies"].values():
            if v["model_id"] == model_id and v["field_changed"] == field_changed and v["status"] == "pending":
                v["sources_json"] = sources_json
                v["discrepancy_score"] = discrepancy_score
                v["updated_at"] = datetime.now().isoformat()
                return _DiscShim(v)
        did = _state["next_discrepancy_id"]
        _state["next_discrepancy_id"] += 1
        row = {
            "id": did, "model_id": model_id, "field_changed": field_changed,
            "sources_json": sources_json, "discrepancy_score": discrepancy_score,
            "status": "pending", "resolved_value": None, "resolved_note": None,
            "resolved_at": None,
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
        }
        _state["discrepancies"][did] = row
        return _DiscShim(row)

    async def resolve_cross_source_discrepancy(session, discrepancy_id,
                                                  resolved_value, resolved_note=None, ignored=False):
        row = _state["discrepancies"].get(discrepancy_id)
        if not row:
            return None
        if row["status"] != "pending":
            return _DiscShim(row)
        row["status"] = "ignored" if ignored else "resolved"
        row["resolved_value"] = resolved_value
        row["resolved_note"] = resolved_note
        row["resolved_at"] = datetime.now().isoformat()
        return _DiscShim(row)

    crud.list_cross_source_discrepancies = list_cross_source_discrepancies
    crud.upsert_cross_source_discrepancy = upsert_cross_source_discrepancy
    crud.resolve_cross_source_discrepancy = resolve_cross_source_discrepancy

    # 替换 validator 里的 session_factory: 用 mock 返回 _MockSession
    class _MockSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def commit(self):
            pass

        async def execute(self, stmt):
            return _MockResult(_state["promotions"])

        async def flush(self):
            pass

    class _MockResult:
        def __init__(self, promotions):
            self._promotions = promotions

        def scalars(self):
            return self

        def all(self):
            return [_PromotionShim(p) for p in self._promotions]

    class _MockSessionCtx:
        async def __aenter__(self):
            return _MockSession()

        async def __aexit__(self, *a):
            return False

    def _factory():
        return _MockSessionCtx()

    validator.async_session_factory = _factory

    return _state


class _PromotionShim:
    def __init__(self, data):
        self.id = data["id"]
        self.provider = data["provider"]
        self.status = data["status"]
        self.raw_data = data["raw_data"]


class _DiscShim:
    def __init__(self, data):
        self.id = data["id"]
        self.model_id = data["model_id"]
        self.field_changed = data["field_changed"]
        self.sources_json = data["sources_json"]
        self.discrepancy_score = data["discrepancy_score"]
        self.status = data["status"]
        self.resolved_value = data.get("resolved_value")
        self.resolved_note = data.get("resolved_note")
        self.resolved_at = data.get("resolved_at")
        self.created_at = data.get("created_at")
        self.updated_at = data.get("updated_at")


# ============================================================
# Test 1: _normalize_field_value
# ============================================================
def test_normalize_field_value():
    print("\n=== Test 1: _normalize_field_value ===")
    from intelligence.cross_source_validator import _normalize_field_value

    # tier
    assert _normalize_field_value("tier", "T3") == "T3"
    assert _normalize_field_value("tier", "T9") is None  # 非法
    # capabilities
    assert _normalize_field_value("capabilities", ["code", "math"]) == "code|math"
    assert _normalize_field_value("capabilities", ["math", "code"]) == "code|math"  # 排序
    # context_length
    assert _normalize_field_value("context_length", 32768) == 32768
    assert _normalize_field_value("context_length", "32768") == 32768
    assert _normalize_field_value("context_length", "abc") is None
    # None
    assert _normalize_field_value("tier", None) is None
    print("  [OK] 7 子场景全过")


# ============================================================
# Test 2: _calc_discrepancy_score
# ============================================================
def test_calc_discrepancy_score():
    print("\n=== Test 2: _calc_discrepancy_score ===")
    from intelligence.cross_source_validator import _calc_discrepancy_score

    # 全一致 → 0
    assert _calc_discrepancy_score("tier", {"T3": []}) == 0.0
    # tier 不一致 → 1.0
    assert _calc_discrepancy_score("tier", {"T3": [], "T2": []}) == 1.0
    # capabilities 不一致 → 0.8
    assert _calc_discrepancy_score("capabilities", {"code": [], "math": []}) == 0.8
    # context_length 差距 ≥ 2 倍 → 0.7
    assert _calc_discrepancy_score("context_length", {32768: [], 131072: []}) == 0.7
    # context_length 差距 < 2 倍 → 0.3 (32768 vs 49152 ≈ 1.5x)
    assert _calc_discrepancy_score("context_length", {32768: [], 49152: []}) == 0.3
    # modality 不一致 → 0.5
    assert _calc_discrepancy_score("modality", {"text": [], "vision": []}) == 0.5
    print("  [OK] 6 子场景全过")


# ============================================================
# Test 3: _group_models_by_id
# ============================================================
def test_group_models_by_id():
    print("\n=== Test 3: _group_models_by_id ===")
    from intelligence.cross_source_validator import _group_models_by_id

    records = [
        {"provider": "siliconflow", "source_type": "model_list",
         "model": {"model_id": "qwen-plus", "family": "qwen", "tier": "T2",
                    "capabilities": ["code"], "context_length": 32768, "confidence": 0.9}},
        {"provider": "openrouter", "source_type": "price_aggregator",
         "model": {"model_id": "qwen-plus", "tier": "T3",
                    "capabilities": ["code", "math"], "context_length": 32768, "confidence": 0.85}},
    ]
    grouped = asyncio.run(_group_models_by_id(records))
    assert "qwen-plus" in grouped
    fields = grouped["qwen-plus"]
    # tier: 2 个值 (T2 / T3)
    assert "tier" in fields
    assert len(fields["tier"]) == 2
    # capabilities: 2 个值 (code vs code|math)
    assert len(fields["capabilities"]) == 2
    # context_length: 1 个值 (32768 一致)
    assert len(fields["context_length"]) == 1
    print(f"  [OK] 1 model → {len(fields)} 字段比对")


# ============================================================
# Test 4: 端到端 — 全源一致 (无分歧)
# ============================================================
def test_detect_no_discrepancy():
    print("\n=== Test 4: 端到端 - 全源一致 ===")
    state = _patch_db_with_in_memory()
    from intelligence.cross_source_validator import detect_and_record

    # 两个源 tier 都是 T3
    state["promotions"].append({
        "id": 1, "provider": "siliconflow", "status": "active",
        "raw_data": json.dumps({
            "_intelligence_kind": "model_metadata",
            "_source_type": "model_list",
            "models": [{"model_id": "qwen-plus", "tier": "T3", "confidence": 0.9}],
        }),
    })
    state["promotions"].append({
        "id": 2, "provider": "openrouter", "status": "active",
        "raw_data": json.dumps({
            "_intelligence_kind": "model_metadata",
            "_source_type": "price_aggregator",
            "models": [{"model_id": "qwen-plus", "tier": "T3", "confidence": 0.85}],
        }),
    })

    summary = asyncio.run(detect_and_record())
    assert summary["total_models"] == 1
    assert summary["with_discrepancy"] == 0
    assert summary["recorded"] == 0
    print(f"  [OK] 1 model 全一致 → 0 入库")


# ============================================================
# Test 5: 端到端 — tier 不一致
# ============================================================
def test_detect_tier_discrepancy():
    print("\n=== Test 5: 端到端 - tier 不一致 ===")
    state = _patch_db_with_in_memory()
    from intelligence.cross_source_validator import detect_and_record

    state["promotions"].append({
        "id": 1, "provider": "siliconflow", "status": "active",
        "raw_data": json.dumps({
            "_intelligence_kind": "model_metadata",
            "_source_type": "model_list",
            "models": [{"model_id": "deepseek-v3", "tier": "T3", "confidence": 0.9}],
        }),
    })
    state["promotions"].append({
        "id": 2, "provider": "openrouter", "status": "active",
        "raw_data": json.dumps({
            "_intelligence_kind": "model_metadata",
            "_source_type": "price_aggregator",
            "models": [{"model_id": "deepseek-v3", "tier": "T2", "confidence": 0.85}],
        }),
    })

    summary = asyncio.run(detect_and_record())
    assert summary["total_models"] == 1
    assert summary["with_discrepancy"] == 1
    assert summary["recorded"] == 1
    # 验证入库内容
    rows = [v for v in state["discrepancies"].values()]
    assert len(rows) == 1
    assert rows[0]["model_id"] == "deepseek-v3"
    assert rows[0]["field_changed"] == "tier"
    assert rows[0]["discrepancy_score"] == 1.0
    # sources_json 应含两个源
    sources = json.loads(rows[0]["sources_json"])
    assert len(sources) == 2
    print(f"  [OK] 1 model tier 不一致 → 1 入库, score=1.0")


# ============================================================
# Test 6: upsert 覆盖已有 pending
# ============================================================
def test_upsert_overwrite_pending():
    print("\n=== Test 6: upsert 覆盖已有 pending ===")
    state = _patch_db_with_in_memory()
    from db import crud

    async def _run():
        class _S:
            pass

        # 第一次入库
        r1 = await crud.upsert_cross_source_discrepancy(
            _S(), "m1", "tier", '[{"source":"a","value":"T3"}]', 0.9
        )
        # 第二次同 model+field+pending → 应覆盖
        r2 = await crud.upsert_cross_source_discrepancy(
            _S(), "m1", "tier", '[{"source":"b","value":"T2"}]', 0.5
        )
        return r1, r2

    r1, r2 = asyncio.run(_run())
    assert r1.id == r2.id, "应覆盖同一行"
    assert r2.discrepancy_score == 0.5
    sources = json.loads(r2.sources_json)
    assert sources[0]["source"] == "b"
    print("  [OK] pending 同 model+field 覆盖")


# ============================================================
# Test 7: resolve - resolved 路径
# ============================================================
def test_resolve_resolved():
    print("\n=== Test 7: resolve - resolved 路径 ===")
    state = _patch_db_with_in_memory()
    from db import crud

    async def _run():
        class _S:
            pass

        await crud.upsert_cross_source_discrepancy(
            _S(), "m1", "tier", '[{"value":"T3"}]', 0.9
        )
        return await crud.resolve_cross_source_discrepancy(_S(), 1, "T3", "取信源A")

    row = asyncio.run(_run())
    assert row.status == "resolved"
    assert row.resolved_value == "T3"
    assert row.resolved_note == "取信源A"
    assert row.resolved_at is not None
    print("  [OK] resolve 路径: status=resolved, resolved_value=T3")


# ============================================================
# Test 8: resolve - ignored 路径
# ============================================================
def test_resolve_ignored():
    print("\n=== Test 8: resolve - ignored 路径 ===")
    state = _patch_db_with_in_memory()
    from db import crud

    async def _run():
        class _S:
            pass

        await crud.upsert_cross_source_discrepancy(_S(), "m1", "tier", '[]', 0.5)
        return await crud.resolve_cross_source_discrepancy(_S(), 1, "(ignored)", "误报", ignored=True)

    row = asyncio.run(_run())
    assert row.status == "ignored"
    assert row.resolved_value == "(ignored)"
    print("  [OK] ignored 路径: status=ignored")


# ============================================================
# Test 9: resolve 幂等
# ============================================================
def test_resolve_idempotent():
    print("\n=== Test 9: resolve 幂等 ===")
    state = _patch_db_with_in_memory()
    from db import crud

    async def _run():
        class _S:
            pass

        await crud.upsert_cross_source_discrepancy(_S(), "m1", "tier", '[]', 0.5)
        r1 = await crud.resolve_cross_source_discrepancy(_S(), 1, "T3")
        r2 = await crud.resolve_cross_source_discrepancy(_S(), 1, "T4")  # 应保留 r1 的值
        return r1, r2

    r1, r2 = asyncio.run(_run())
    assert r1.status == "resolved"
    assert r2.status == "resolved"
    assert r2.resolved_value == "T3", "第二次 resolve 不应覆盖"
    print("  [OK] resolve 重复调用幂等")


# ============================================================
# Test 10: 1 model 仅 1 源 (数据稀疏) → 跳过
# ============================================================
def test_detect_sparse_skip():
    print("\n=== Test 10: 1 model 仅 1 源跳过 ===")
    state = _patch_db_with_in_memory()
    from intelligence.cross_source_validator import detect_and_record

    # 只 1 个源提到该 model
    state["promotions"].append({
        "id": 1, "provider": "siliconflow", "status": "active",
        "raw_data": json.dumps({
            "_intelligence_kind": "model_metadata",
            "_source_type": "model_list",
            "models": [{"model_id": "lone-model", "tier": "T3"}],
        }),
    })

    summary = asyncio.run(detect_and_record())
    assert summary["total_models"] == 1
    assert summary["with_discrepancy"] == 0  # 数据稀疏，不做对比
    print("  [OK] 1 源孤立 model 不做对比")


# ============================================================
# Test 11 (P0-7 fix): 并发 INSERT 撞 UniqueConstraint → fallback 重新 SELECT+UPDATE
# ============================================================
def test_upsert_integrity_error_fallback():
    """模拟 2 个 worker 同时 upsert 同一 (model, field, pending):
    - worker A: SELECT None → INSERT 成功
    - worker B: SELECT None → INSERT 撞 UniqueConstraint → IntegrityError
    - worker B (real upsert): rollback → 重 SELECT 拿到 A 的行 → UPDATE

    验证点: 不会产生 2 条 pending 行, worker B 拿到的是 A 的行 (id 相同).
    """
    print("\n=== Test 11: P0-7 IntegrityError fallback → 同一行 UPDATE ===")
    from sqlalchemy.exc import IntegrityError

    class _Row:
        """模拟 winner 已 INSERT 成功的行."""
        def __init__(self, did, model_id, field_changed):
            self.id = did
            self.model_id = model_id
            self.field_changed = field_changed
            self.status = "pending"
            self.sources_json = "[]"
            self.discrepancy_score = 0.0

    # 控制 session 行为:
    # 1) 第一次 SELECT → None (模拟 worker B 还没看到 winner)
    # 2) session.flush() → 掊 IntegrityError (UniqueConstraint)
    # 3) session.rollback() 调用
    # 4) 第二次 SELECT → 返回 winner 行
    # 5) 后面 session.flush() → 成功
    winner_row = _Row(did=99, model_id="race-model", field_changed="tier")

    flush_count = {"n": 0}
    select_count = {"n": 0}
    rollback_count = {"n": 0}

    class _RacingSession:
        async def execute(self, stmt):
            select_count["n"] += 1
            # 第一次 SELECT 看不到 winner, 第二次 SELECT 可看到
            if select_count["n"] == 1:
                return _MockResult([None])  # scalar_one_or_none() 返回 None
            else:
                return _MockResult([winner_row])  # 拿到 winner 的行

        async def flush(self):
            flush_count["n"] += 1
            if flush_count["n"] == 1:
                # 第一次 flush (INSERT) 撞 UniqueConstraint
                raise IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed"))

        async def rollback(self):
            rollback_count["n"] += 1

        def add(self, _row):
            # SQLAlchemy session.add 是同步的, real crud 函数也是同步调
            pass

        async def commit(self):
            pass

    class _MockResult:
        def __init__(self, rows):
            # rows 是 [Row] 或 [None]
            self._rows = rows

        def scalars(self):
            return self

        def scalar_one_or_none(self):
            return self._rows[0] if self._rows else None

    async def _run():
        # 调用原始函数 (不走 mock upsert)
        from db import crud
        # 恢复原始函数: crud.upsert_cross_source_discrepancy 已被 mock 覆盖,
        # 调用 _ORIGINAL_UPSERT 走原始逻辑
        return await _ORIGINAL_UPSERT(
            _RacingSession(),
            model_id="race-model",
            field_changed="tier",
            sources_json='[{"source":"b","value":"T2"}]',
            discrepancy_score=0.5,
        )

    row = asyncio.run(_run())
    # 验证 fallback 路线被走到
    assert rollback_count["n"] == 1, (
        f"IntegrityError 后应 rollback 1 次, 实际 {rollback_count['n']}"
    )
    assert select_count["n"] == 2, (
        f"SELECT 应 2 次 (1+1 fallback), 实际 {select_count['n']}"
    )
    assert flush_count["n"] == 2, (
        f"flush 应 2 次 (1 INSERT撞错 + 1 UPDATE), 实际 {flush_count['n']}"
    )
    # 拿到的是 winner 的 id (99), 不是新建的
    assert row.id == 99, (
        f"fallback 后应拿到 winner 行 (id=99), 实际 id={row.id}"
    )
    # UPDATE 应覆盖了 sources_json / score
    assert row.sources_json == '[{"source":"b","value":"T2"}]'
    assert row.discrepancy_score == 0.5
    print("  [OK] 并发 INSERT 撞 UniqueConstraint → rollback + re-SELECT + UPDATE 同 winner")

    # Test 11b: 极小概率 case — INSERT 撞错但 winner 已 resolve,
    # fallback 重新 SELECT 也找不到 pending → 函数返 None
    class _GoneWinRacingSession:
        async def execute(self, stmt):
            select_count["n"] += 1
            return _MockResult([None])  # 两次都看不到

        async def flush(self):
            flush_count["n"] += 1
            if flush_count["n"] == 1:
                raise IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed"))

        async def rollback(self):
            rollback_count["n"] += 1

        def add(self, _row):
            pass

        async def commit(self):
            pass

    # 重置计数
    flush_count["n"] = 0
    select_count["n"] = 0
    rollback_count["n"] = 0

    result = asyncio.run(_ORIGINAL_UPSERT(
        _GoneWinRacingSession(),
        model_id="gone-model",
        field_changed="tier",
        sources_json="[]",
        discrepancy_score=0.3,
    ))
    assert result is None, (
        f"winner 已 resolve 时 fallback SELECT 也找不到, 应返 None, 实际 {result}"
    )
    print("  [OK] 极小概率: winner 已 resolve → fallback 返 None, 不崩溃")


# ============================================================
# Main
# ============================================================
def main():
    print("=" * 60)
    print("Phase 4 E3 — 跨源交叉验证 测试")
    print("=" * 60)

    test_normalize_field_value()
    test_calc_discrepancy_score()
    test_group_models_by_id()
    test_detect_no_discrepancy()
    test_detect_tier_discrepancy()
    test_upsert_overwrite_pending()
    test_resolve_resolved()
    test_resolve_ignored()
    test_resolve_idempotent()
    test_detect_sparse_skip()
    # P0-7 新增: IntegrityError fallback
    test_upsert_integrity_error_fallback()

    print("\n" + "=" * 60)
    print("ALL 11 TESTS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    main()