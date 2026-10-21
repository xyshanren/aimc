"""V6.17d 模型组语义准入 + 成员位次语义 (2026-10-21)

覆盖:
1. gateway/group_rules.group_runtime_accepts —— 运行时层准入判定
   (tier 精确匹配 / capabilities 子集 / min_context / require_vision)
2. group_candidate_models —— 看板候选列表 (与路由同源 + 策展 tier 白名单)
3. dashboard/group_members —— priority 语义 (同族旧行继承 / 队尾追加 / 显式值)
4. 看板 API: GET /dashboard/api/registry/groups 返回 candidates + suggested_priority;
   POST 加成员带 priority / 继承 priority; PATCH 改位次
5. 前端接线断言 (app.js 用服务端 candidates, 不再硬编码 tier 白名单)

跑法: python -m pytest tests/test_v617d_group_admission.py -q
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import types
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_TMP_DIR = None


def _install_mock_env():
    """config mock (与 test_v6x_onboarding 同款): 临时 sqlite 库"""
    global _TMP_DIR
    if _TMP_DIR is None:
        _TMP_DIR = tempfile.mkdtemp(prefix="aimc_v617d_")
    db_path = os.path.join(_TMP_DIR, "test.db")
    db_url = f"sqlite+aiosqlite:///{db_path}"

    sys.modules.setdefault("config", types.ModuleType("config"))
    sys.modules["config"].settings = types.SimpleNamespace(
        openai_api_key=os.environ.get("AIMC_TEST_PARSE_KEY", ""),
        openai_base_url="https://mock.local/v1",
        parse_model="gpt-4o-mini",
        crawl_enabled=False,
        aimc_master_key="a" * 64,
        route_refresh_interval=600,
        db_url=db_url,
        db_path=db_path,
        host="127.0.0.1",
        port=8080,
        log_level="info",
    )
    return _TMP_DIR


_install_mock_env()


# ============================================================
# 1) 运行时层准入判定
# ============================================================

def _reg(tier="T1", modality="text", caps=None, ctx=131072):
    return types.SimpleNamespace(
        model_id="m", tier=tier, modality=modality,
        capabilities=caps, context_length=ctx)


def _grp(tier=None, caps=None, min_ctx=0, require_vision=0):
    return types.SimpleNamespace(
        group_id="tier:x", tier=tier, capabilities=caps,
        min_context=min_ctx, require_vision=require_vision)


def test_runtime_accepts_tier_exact_match():
    from gateway.group_rules import group_runtime_accepts
    g = _grp(tier="T2")
    assert group_runtime_accepts(g, _reg(tier="T2"))[0] is True
    ok, reason = group_runtime_accepts(g, _reg(tier="T3"))
    assert ok is False and reason == "tier"


def test_runtime_accepts_capabilities_subset():
    from gateway.group_rules import group_runtime_accepts
    g = _grp(caps='["code_specialized"]')
    assert group_runtime_accepts(g, _reg(caps='["code","code_specialized"]'))[0] is True
    # 泛用模型即便带 'code' 也不满足"代码特化"
    ok, reason = group_runtime_accepts(g, _reg(caps='["general","code"]'))
    assert ok is False and reason == "capabilities"


def test_runtime_accepts_min_context():
    from gateway.group_rules import group_runtime_accepts
    g = _grp(min_ctx=64000)
    assert group_runtime_accepts(g, _reg(ctx=131072))[0] is True
    ok, reason = group_runtime_accepts(g, _reg(ctx=32768))
    assert ok is False and reason == "min_context"
    ok, reason = group_runtime_accepts(g, _reg(ctx=None))
    assert ok is False and reason == "min_context"


def test_runtime_accepts_require_vision_multimodal_ok():
    from gateway.group_rules import group_runtime_accepts
    g = _grp(require_vision=1)
    assert group_runtime_accepts(g, _reg(modality="multimodal"))[0] is True
    assert group_runtime_accepts(g, _reg(modality="vision"))[0] is True
    ok, reason = group_runtime_accepts(g, _reg(modality="text"))
    assert ok is False and reason == "require_vision"


def test_router_uses_shared_predicate():
    """路由层必须调共享判定 (否则看板与路由会漂移)"""
    src = (PROJECT_ROOT / "gateway" / "router.py").read_text(encoding="utf-8")
    assert "group_runtime_accepts" in src, "路由层应改用 gateway.group_rules.group_runtime_accepts"


# ============================================================
# 2) 候选列表 (看板与路由同源)
# ============================================================

def test_candidates_vision_group_only_multimodal():
    from gateway.group_rules import group_candidate_models
    g = _grp(require_vision=1)
    g.group_id = "tier:vision"
    rows = [
        types.SimpleNamespace(model_id="m-mm", tier="T1", modality="multimodal",
                              capabilities="[]", context_length=131072, status="active"),
        types.SimpleNamespace(model_id="m-text", tier="T1", modality="text",
                              capabilities="[]", context_length=131072, status="active"),
        types.SimpleNamespace(model_id="m-dep", tier="T1", modality="multimodal",
                              capabilities="[]", context_length=131072, status="deprecated"),
        types.SimpleNamespace(model_id="m-embed", tier="T1", modality="text",
                              capabilities="[]", context_length=131072, status="active"),
    ]
    cands = group_candidate_models(g, rows)
    ids = [c["model_id"] for c in cands]
    assert "m-mm" in ids
    assert "m-text" not in ids          # 纯文本 → 运行时会 skip (死成员)
    assert "m-dep" not in ids           # deprecated
    assert "m-embed" not in ids         # 向量类非聊天模型


def test_candidates_code_group_requires_specialized():
    from gateway.group_rules import group_candidate_models
    g = _grp(caps='["code_specialized"]')
    g.group_id = "tier:code"
    rows = [
        types.SimpleNamespace(model_id="coder-x", tier="T1", modality="text",
                              capabilities='["code","code_specialized"]',
                              context_length=262144, status="active"),
        types.SimpleNamespace(model_id="general-y", tier="T0", modality="text",
                              capabilities='["general","code"]',
                              context_length=262144, status="active"),
    ]
    ids = [c["model_id"] for c in group_candidate_models(g, rows)]
    assert ids == ["coder-x"]


def test_candidates_tier_policy_whitelist():
    """策展层 tier 白名单: 视觉组只收 T0/T1, 轻量视觉组收 T2+"""
    from gateway.group_rules import group_candidate_models
    g = _grp(require_vision=1)
    g.group_id = "tier:vision"
    rows = [
        types.SimpleNamespace(model_id="v-t1", tier="T1", modality="vision",
                              capabilities="[]", context_length=131072, status="active"),
        types.SimpleNamespace(model_id="v-t3", tier="T3", modality="vision",
                              capabilities="[]", context_length=131072, status="active"),
    ]
    assert [c["model_id"] for c in group_candidate_models(g, rows)] == ["v-t1"]

    g2 = _grp(require_vision=1)
    g2.group_id = "tier:vision-light"
    # 轻量视觉组收 T2+ → T1 的 v-t1 不收, T3 的 v-t3 收
    assert [c["model_id"] for c in group_candidate_models(g2, rows)] == ["v-t3"]


def test_candidates_exclude_existing_members():
    from gateway.group_rules import group_candidate_models
    g = _grp()
    g.group_id = "tier:strong"
    rows = [
        types.SimpleNamespace(model_id="a", tier="T1", modality="text",
                              capabilities="[]", context_length=131072, status="active"),
        types.SimpleNamespace(model_id="b", tier="T1", modality="text",
                              capabilities="[]", context_length=131072, status="active"),
    ]
    ids = [c["model_id"] for c in group_candidate_models(g, rows, ["a"])]
    assert ids == ["b"]


# ============================================================
# 3) priority 语义
# ============================================================

def test_normalize_model_key_strips_preview_suffix():
    from dashboard.group_members import normalize_model_key
    assert normalize_model_key("minimax-m3.1-flash-preview") == "minimax-m3.1-flash"
    assert normalize_model_key("minimax-m3.1-flash") == "minimax-m3.1-flash"
    assert normalize_model_key("qwen3-coder-30b-a3b-instruct") == "qwen3-coder-30b-a3b-instruct"


def test_sibling_priority_inherited_for_renamed_model():
    from dashboard.group_members import build_sibling_priority_map, suggest_priority
    rows = [
        types.SimpleNamespace(model_id="minimax-m3.1-flash-preview", priority=90,
                              is_active=0, removed_at=None, created_at=None, id=1),
        types.SimpleNamespace(model_id="glm-5.3-flash", priority=100,
                              is_active=1, removed_at=None, created_at=None, id=2),
        # 同名现役行 (同族 key 相同) —— 不能覆盖历史排位
        types.SimpleNamespace(model_id="minimax-m3.1-flash", priority=125,
                              is_active=1, removed_at=None, created_at=None, id=3),
    ]
    smap = build_sibling_priority_map(rows)
    # 改名模型继承旧行排位 (90), 不被无声追加到队尾
    assert suggest_priority("minimax-m3.1-flash", smap, [100, 120]) == 90
    # 无同族旧行 → 组内最大值 +5 (追加队尾)
    assert suggest_priority("brand-new-model", smap, [100, 120]) == 125
    assert suggest_priority("brand-new-model", {}, []) == 100


# ============================================================
# 4) 看板 API
# ============================================================

def _seed_rows():
    """写入最小 registry + group 数据 (幂等: 多用例共用同一临时库)"""
    async def run():
        from sqlalchemy import delete, select
        from db.init_db import init_db
        from db.engine import async_session_factory
        from db.models import ModelGroup, ModelGroupMember, ModelRegistry
        await init_db()
        async with async_session_factory() as session:
            # 清掉本文件造的测试数据 (重复执行不撞唯一约束)
            await session.execute(delete(ModelGroupMember).where(
                ModelGroupMember.group_id.in_(["tier:vision", "tier:code"])))
            await session.execute(delete(ModelGroup).where(
                ModelGroup.group_id.in_(["tier:vision", "tier:code"])))
            await session.execute(delete(ModelRegistry).where(
                ModelRegistry.model_id.in_(["m-mm", "m-mm2", "m-text", "coder-x", "general-y"])))
            session.add(ModelRegistry(
                model_id="m-mm", family="x", display_name="m-mm", tier="T1",
                capabilities="[]", context_length=131072, modality="multimodal",
                status="active"))
            session.add(ModelRegistry(
                model_id="m-mm2", family="x", display_name="m-mm2", tier="T1",
                capabilities="[]", context_length=131072, modality="multimodal",
                status="active"))
            session.add(ModelRegistry(
                model_id="m-text", family="x", display_name="m-text", tier="T1",
                capabilities="[]", context_length=131072, modality="text",
                status="active"))
            session.add(ModelRegistry(
                model_id="coder-x", family="x", display_name="coder-x", tier="T1",
                capabilities='["code","code_specialized"]', context_length=262144,
                modality="text", status="active"))
            session.add(ModelRegistry(
                model_id="general-y", family="x", display_name="general-y", tier="T1",
                capabilities='["general"]', context_length=131072, modality="text",
                status="active"))
            session.add(ModelGroup(
                group_id="tier:vision", display_name="视觉组", tier=None,
                capabilities=None, min_context=0, require_vision=1,
                strategy="cheapest", fallback_group=None, enabled=1, sort_order=25))
            session.add(ModelGroup(
                group_id="tier:code", display_name="代码组", tier=None,
                capabilities='["code_specialized"]', min_context=0, require_vision=0,
                strategy="cheapest", fallback_group=None, enabled=1, sort_order=50))
            session.add(ModelGroupMember(
                group_id="tier:vision", model_id="m-mm", priority=100,
                weight=1.0, added_by="manual", is_active=1))
            # 同族旧行 (软删) —— 改名模型加回时应继承其 priority
            session.add(ModelGroupMember(
                group_id="tier:vision", model_id="m-mm2-preview", priority=90,
                weight=1.0, added_by="manual", is_active=0))
            await session.commit()
    asyncio.run(run())


def _client():
    import httpx
    from fastapi import FastAPI
    from dashboard.routes import router as dash_router
    from dashboard.onboarding import router as onboarding_router
    app = FastAPI()
    app.include_router(dash_router)
    app.include_router(onboarding_router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://testserver")


def test_registry_groups_returns_candidates_and_priority():
    _seed_rows()

    async def run():
        async with _client() as c:
            resp = await c.get("/dashboard/api/registry/groups")
            assert resp.status_code == 200, resp.text
            data = resp.json()
            groups = {g["group_id"]: g for g in data["groups"]}
            vis = groups["tier:vision"]
            ids = [x["model_id"] for x in vis["candidates"]]
            assert ids == ["m-mm2"]           # 只收多模态且非成员
            assert "m-mm" not in ids          # 已是成员
            assert "m-text" not in ids        # 纯文本 → 运行时会 skip
            assert "coder-x" not in ids       # 纯文本 (虽 T1) → 不该在视觉组
            assert "general-y" not in ids
            assert vis["suggested_priority"] == 105   # 组内 100 → +5
            code = groups["tier:code"]
            code_ids = [x["model_id"] for x in code["candidates"]]
            assert code_ids == ["coder-x"]    # 仅代码特化
            assert code["suggested_priority"] == 100
    asyncio.run(run())


def test_add_member_priority_semantics():
    _seed_rows()

    async def run():
        async with _client() as c:
            # 显式 priority
            resp = await c.post("/dashboard/api/onboarding/groups/tier:vision/members",
                                json={"model_id": "m-mm", "priority": 77})
            assert resp.status_code == 200, resp.text
            assert resp.json()["priority"] == 77
            # 已是成员 + 显式 priority → 幂等改位次
            resp = await c.post("/dashboard/api/onboarding/groups/tier:vision/members",
                                json={"model_id": "m-mm", "priority": 66})
            assert resp.status_code == 200, resp.text
            assert resp.json().get("updated") is True
            # 已是成员 + 无 priority → 409
            resp = await c.post("/dashboard/api/onboarding/groups/tier:vision/members",
                                json={"model_id": "m-mm"})
            assert resp.status_code == 409
            # PATCH 改位次
            resp = await c.patch(
                "/dashboard/api/onboarding/groups/tier:vision/members/m-mm",
                json={"priority": 55})
            assert resp.status_code == 200, resp.text
            assert resp.json()["priority"] == 55
            # 不存在的成员 → 404
            resp = await c.patch(
                "/dashboard/api/onboarding/groups/tier:vision/members/nope",
                json={"priority": 55})
            assert resp.status_code == 404
    asyncio.run(run())


def test_add_member_inherits_sibling_priority():
    """改名模型加回同组 → 继承同族旧行的 priority (不追加队尾)"""
    _seed_rows()

    async def run():
        async with _client() as c:
            resp = await c.post("/dashboard/api/onboarding/groups/tier:vision/members",
                                json={"model_id": "m-mm2"})
            assert resp.status_code == 200, resp.text
            async with _session() as s:
                row = await _member_row(s, "tier:vision", "m-mm2")
                assert row is not None
                # 同族旧行 (m-mm-preview, priority=90) 存在 → 继承 90
                assert row.priority == 90
    asyncio.run(run())


def _session():
    from db.engine import async_session_factory
    return async_session_factory()


async def _member_row(session, group_id, model_id):
    from sqlalchemy import select
    from db.models import ModelGroupMember
    return (await session.execute(
        select(ModelGroupMember).where(
            ModelGroupMember.group_id == group_id,
            ModelGroupMember.model_id == model_id)
    )).scalars().first()


# ============================================================
# 5) 前端接线
# ============================================================

def test_frontend_uses_server_candidates():
    src = (PROJECT_ROOT / "dashboard" / "static" / "app.js").read_text(encoding="utf-8")
    assert "g.candidates" in src, "加成员下拉应使用服务端算好的候选"
    assert "_tierAllowedForGroup" not in src, \
        "前端不再硬编码 tier 白名单 (规则在服务端 gateway/group_rules)"
    assert "ob-add-priority" in src, "加成员行应有 priority 输入框"
    assert "saveGroupMemberPriority" in src, "成员表应支持内联改 priority"
