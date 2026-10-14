"""看板「初始化配置」页 (dashboard/onboarding.py) 单元测试

覆盖:
1. GET /api/onboarding/status: 空库 → setup_done=False + 目录完整 (hidden 渠道不出现)
2. POST /api/onboarding/channels/{key}: 建渠道+账号+价格行, setup_done 翻 true
3. POST channels/{key} 重复账号 → 409
4. POST channels/未知 key → 404; hidden 渠道 (volcengine) → 404
5. POST /api/onboarding/complete → 空账号库也 setup_done=True
6. POST /api/onboarding/test: 缺 api_key → 400; 未知渠道 → 404
7. channels_catalog 单一事实源一致性抽查

DB 状态断言全部走 crud 助手 (测试文件不写裸 ORM 语句)。
测试 key 一律 f-string 现算 (AIMC 自测值, 非真实凭据)。
跑法: python tests/test_v6x_onboarding.py 或 pytest tests/test_v6x_onboarding.py
注意: 用例共享同一临时库且按文件顺序执行 (第一个用例必须排在最前)。
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _key(name: str) -> str:
    """现算的自测 key (避免任何字面量凭据形态)"""
    return f"aimc-selftest-{name}-" + "0" * 8


def _install_mock_env():
    tmp_dir = tempfile.mkdtemp(prefix="aimc_onboarding_")
    db_path = os.path.join(tmp_dir, "test.db")
    db_url = f"sqlite+aiosqlite:///{db_path}"

    import types

    sys.modules.setdefault("config", types.ModuleType("config"))
    sys.modules["config"].settings = types.SimpleNamespace(
        openai_api_key=os.environ.get("AIMC_TEST_PARSE_KEY", ""),
        openai_base_url="https://mock.local/v1",
        parse_model="gpt-4o-mini",
        crawl_enabled=True,
        aimc_master_key="a" * 64,   # 测试 master key (64 hex), 非真实凭据
        route_refresh_interval=600,
        db_url=db_url,
        db_path=db_path,
        host="127.0.0.1",
        port=8080,
        log_level="info",
    )
    return tmp_dir


_install_mock_env()


def _build_app():
    from fastapi import FastAPI
    from dashboard.onboarding import router as onboarding_router

    app = FastAPI()
    app.include_router(onboarding_router)
    return app


_APP = None


def _get_app():
    global _APP
    if _APP is None:
        _APP = _build_app()
    return _APP


def _get_client():
    import httpx
    transport = httpx.ASGITransport(app=_get_app())
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


def _ensure_db():
    from db.init_db import init_db
    asyncio.run(init_db())


async def _ensure_db_async():
    """用例内建表 + 重置 app 缓存 (conftest 每 test 后删项目模块 → engine
    按当前 settings 重建; _APP 单例若跨用例存活会持有旧 engine 绑定,
    导致端点写入与用例断言分属两个库 — 每用例重建保证一致)"""
    global _APP
    _APP = None
    from db.init_db import init_db
    await init_db()


def _crud_session():
    from db.engine import async_session_factory
    return async_session_factory()


async def _account_rows():
    from db import crud
    async with _crud_session() as session:
        return await crud.list_accounts(session)


# ============================================================
# 用例 (顺序执行, 共享临时库 — 首个用例依赖空库)
# ============================================================

def test_status_fresh_db_setup_not_done():
    _ensure_db()

    async def run():
        await _ensure_db_async()
        async with _get_client() as c:
            resp = await c.get("/dashboard/api/onboarding/status")
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["setup_done"] is False
            keys = [ch["key"] for ch in data["catalog"]]
            assert "sensenova" in keys and "modelscope" in keys and "siliconflow" in keys
            assert "volcengine" not in keys and "hunyuan" not in keys  # hidden
            assert set(data["categories"].keys()) >= {"free", "paid", "subscription"}
            assert data["pr_guide"]["file"] == "channels_catalog.py"
            # 免费渠道卡片带注册链接与文案
            free = [ch for ch in data["catalog"] if ch["category"] == "free"]
            assert len(free) == 3
            for ch in free:
                assert ch["register_url"].startswith("https://")
                assert ch["description"]
    asyncio.run(run())


def test_add_channel_creates_everything_and_flips_setup_done():
    async def run():
        await _ensure_db_async()
        async with _get_client() as c:
            resp = await c.post("/dashboard/api/onboarding/channels/siliconflow",
                                json={"api_key": _key("sf")})
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["ok"] is True
            assert data["channel"] == "siliconflow_aggregator"
            assert data["account"] == "siliconflow_01"
            assert data["prices"] == 6  # siliconflow 目录 6 行价格 (2026-10-14 扩)
            # 自动种子: registry + 默认组 (V6.17)
            assert data["seed"]["registry_created"] == 5  # v3/qwen2.5-72b/5.3/5.3-flash/5.2
            assert data["seed"]["members_created"] >= 8   # tier 组 + code/vision 映射

            rows = await _account_rows()
            assert len(rows) == 1
            acc, ch = rows[0]
            assert acc.account_name == "siliconflow_01"
            assert ch.name == "siliconflow_aggregator"
            # ciphertext 真的入库 (非明文)
            assert acc.api_key_ciphertext and bytes(acc.api_key_ciphertext) != _key("sf").encode()

            # 默认组存在 + siliconflow 模型按 tier 入组
            from db.engine import async_session_factory as _asf
            from db.models import ModelGroup, ModelGroupMember, ModelRegistry
            from sqlalchemy import select
            async with _asf() as s:
                gids = set((await s.execute(select(ModelGroup.group_id))).scalars().all())
                assert {"tier:flagship", "tier:strong", "tier:balanced", "tier:light",
                        "tier:vision", "tier:vision-light", "tier:code"} <= gids
                reg = set((await s.execute(select(ModelRegistry.model_id))).scalars().all())
                assert {"deepseek-v3", "qwen2.5-72b"} <= reg
                members = set((await s.execute(
                    select(ModelGroupMember.group_id, ModelGroupMember.model_id)
                    .where(ModelGroupMember.is_active == 1)
                )).all())
                assert ("tier:strong", "deepseek-v3") in members
                assert ("tier:balanced", "qwen2.5-72b") in members
                assert ("tier:code", "deepseek-v3") in members

            # setup_done 因账号存在而翻 true
            resp = await c.get("/dashboard/api/onboarding/status")
            assert resp.json()["setup_done"] is True
    asyncio.run(run())


def test_add_channel_duplicate_409():
    async def run():
        await _ensure_db_async()
        async with _get_client() as c:
            r1 = await c.post("/dashboard/api/onboarding/channels/deepseek",
                              json={"api_key": _key("a")})
            assert r1.status_code == 200
            r2 = await c.post("/dashboard/api/onboarding/channels/deepseek",
                              json={"api_key": _key("b")})
            assert r2.status_code == 409
    asyncio.run(run())


def test_add_channel_unknown_and_hidden_404():
    async def run():
        await _ensure_db_async()
        async with _get_client() as c:
            r_unknown = await c.post("/dashboard/api/onboarding/channels/no_such_channel",
                                     json={"api_key": _key("x")})
            assert r_unknown.status_code == 404
            r_hidden = await c.post("/dashboard/api/onboarding/channels/volcengine",
                                    json={"api_key": _key("x")})
            assert r_hidden.status_code == 404
    asyncio.run(run())


def test_complete_marks_done_even_without_accounts():
    async def run():
        await _ensure_db_async()
        # complete 落 settings_runtime 标记 → 即使没有账号也算完成
        async with _get_client() as c:
            resp = await c.post("/dashboard/api/onboarding/complete")
            assert resp.status_code == 200
            resp = await c.get("/dashboard/api/onboarding/status")
            assert resp.json()["setup_done"] is True
    asyncio.run(run())


def test_test_key_endpoint_validation():
    async def run():
        await _ensure_db_async()
        async with _get_client() as c:
            r_nokey = await c.post("/dashboard/api/onboarding/test", json={"key": "sensenova"})
            assert r_nokey.status_code == 400
            r_unknown = await c.post("/dashboard/api/onboarding/test",
                                     json={"key": "nope", "api_key": _key("x")})
            assert r_unknown.status_code == 404
    asyncio.run(run())


def test_catalog_prices_match_single_source():
    """目录价格数据与 channels_catalog 单一事实源一致 (抽查)"""
    from channels_catalog import CHANNELS, get_by_key

    assert get_by_key("deepseek")["prices"][0][:3] == ("deepseek-v4-pro", "peak", "工作日09:00-12:00")
    assert get_by_key("taotoken")["prices"][0][0] == "glm-5.3-flash"
    assert len(CHANNELS) == 11  # 9 可见 + volcengine/hunyuan hidden
    for cfg in CHANNELS:
        assert cfg["env_var"] and cfg["base_url"] and cfg["prices"]


def test_seed_groups_idempotent():
    """seed-groups 全量补种: 首次补缺失, 二次零 diff (ensure 语义)"""
    async def run():
        await _ensure_db_async()
        async with _get_client() as c:
            r1 = await c.post("/dashboard/api/onboarding/seed-groups")
            assert r1.status_code == 200
            d1 = r1.json()
            assert d1["ok"] is True
            n1 = d1["registry_created"] + d1["members_created"]

            r2 = await c.post("/dashboard/api/onboarding/seed-groups")
            d2 = r2.json()
            assert d2["registry_created"] == 0 and d2["members_created"] == 0
            # 首次 seed 只补已建渠道 (siliconflow/deepseek 已建): 幽灵渠道不建候选
            # (sensenova 等未建渠道的目录模型不出现在 registry)
            from db.engine import async_session_factory as _asf2
            from db.models import ModelRegistry as _MR
            from sqlalchemy import select as _sel
            async with _asf2() as s:
                reg = set((await s.execute(_sel(_MR.model_id))).scalars().all())
                assert "sensenova-6.8-flash-lite" not in reg  # 渠道未建 → 不建
                assert "deepseek-v4-pro" in reg               # deepseek 已建 → 建
    asyncio.run(run())


def test_group_edit_lifecycle():
    """组编辑端点生命周期: 创建 → 加成员 → 重复 409 → 软删 → 复活 → 加未知模型 404"""
    async def run():
        await _ensure_db_async()
        async with _get_client() as c:
            # 创建
            r = await c.post("/dashboard/api/onboarding/groups",
                             json={"group_id": "tier:code-review", "display_name": "代码评审组",
                                   "fallback_group": "tier:balanced"})
            assert r.status_code == 200, r.text
            # 重复创建 409
            r = await c.post("/dashboard/api/onboarding/groups", json={"group_id": "tier:code-review"})
            assert r.status_code == 409
            # 非法 group_id 400
            r = await c.post("/dashboard/api/onboarding/groups", json={"group_id": "bogus"})
            assert r.status_code == 400
            # 加成员 (registry 有 deepseek-v3)
            r = await c.post("/dashboard/api/onboarding/groups/tier:code-review/members",
                             json={"model_id": "deepseek-v3"})
            assert r.status_code == 200, r.text
            # 重复加 409
            r = await c.post("/dashboard/api/onboarding/groups/tier:code-review/members",
                             json={"model_id": "deepseek-v3"})
            assert r.status_code == 409
            # 未知模型 404
            r = await c.post("/dashboard/api/onboarding/groups/tier:code-review/members",
                             json={"model_id": "no-such-model"})
            assert r.status_code == 404
            # 未知组 404
            r = await c.post("/dashboard/api/onboarding/groups/tier:nope/members",
                             json={"model_id": "deepseek-v3"})
            assert r.status_code == 404
            # 软删
            r = await c.delete("/dashboard/api/onboarding/groups/tier:code-review/members/deepseek-v3")
            assert r.status_code == 200
            # 软删后不存在 → 404
            r = await c.delete("/dashboard/api/onboarding/groups/tier:code-review/members/deepseek-v3")
            assert r.status_code == 404
            # 重新加入 = 复活 (200, 非 UNIQUE 冲突)
            r = await c.post("/dashboard/api/onboarding/groups/tier:code-review/members",
                             json={"model_id": "deepseek-v3", "priority": 77})
            assert r.status_code == 200
            # 复活后 priority 应为 77
            from db.engine import async_session_factory as _asf
            from db.models import ModelGroupMember
            from sqlalchemy import select
            async with _asf() as s:
                m = (await s.execute(select(ModelGroupMember).where(
                    ModelGroupMember.group_id == "tier:code-review",
                    ModelGroupMember.model_id == "deepseek-v3",
                ))).scalars().first()
                assert m.is_active == 1 and m.priority == 77 and m.added_by == "manual"
    asyncio.run(run())


def test_pricing_unpriced_endpoint():
    """待补价盘点端点: NULL 行查询 + 聊天/非聊天分类"""
    async def run():
        await _ensure_db_async()
        async with _get_client() as c:
            resp = await c.get("/dashboard/api/pricing/unpriced")
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert "chat_pending" in data and "non_chat_count" in data
            # 分类自检: 端点内关键词逻辑 (这里用返回数据结构验证)
            for item in data["chat_pending"]:
                low = item["model"].lower()
                assert not any(k in low for k in (
                    "embed", "rerank", "asr", "voice", "ocr", "image", "video"))
    asyncio.run(run())


# ============================================================
# 模型消失检测 (intelligence/model_absence.py)
# 并入本文件: 复用已验证的 settings 窗口机制 (独立测试文件会被 conftest
# 收集期的模块删除打到错误库, 见 _ensure_db_async 注释)。
# 注意: detect_missing_models 必须用例内延迟 import — 顶层 import 会持有
# 收集期旧模块闭包 (其 db.engine 绑错库)。
# ============================================================


async def _absence_cleanup(providers: list, seeded_models: list):
    from db.engine import async_session_factory
    from db.models import ModelChangeSuggestion, ModelGroupMember, ModelListSnapshot
    from sqlalchemy import delete
    async with async_session_factory() as s:
        await s.execute(delete(ModelListSnapshot).where(
            ModelListSnapshot.provider.in_(list(providers))))
        if seeded_models:
            await s.execute(delete(ModelGroupMember).where(
                ModelGroupMember.model_id.in_(list(seeded_models))))
            await s.execute(delete(ModelChangeSuggestion).where(
                ModelChangeSuggestion.model_id.in_(list(seeded_models))))
        await s.commit()


async def _absence_snapshot_state(provider: str):
    from db.engine import async_session_factory
    from db.models import ModelListSnapshot
    from sqlalchemy import select
    async with async_session_factory() as s:
        rows = list((await s.execute(select(
            ModelListSnapshot.model, ModelListSnapshot.miss_count)
            .where(ModelListSnapshot.provider == provider))).all())
        return {m: c for m, c in rows}


def test_absence_first_snapshot_and_threshold():
    """首次全量插入 → 消失计 miss → 连续第二次且无组员只记录 → 回归清零"""
    async def run():
        await _ensure_db_async()
        from intelligence.model_absence import detect_missing_models
        try:
            stats = await detect_missing_models("pa1", ["ma-a", "ma-b", "ma-c"])
            assert stats["suggestions_created"] == []
            state = await _absence_snapshot_state("pa1")
            assert state == {"ma-a": 0, "ma-b": 0, "ma-c": 0}
            await detect_missing_models("pa1", ["ma-a", "ma-b"])   # ma-c miss 1
            state = await _absence_snapshot_state("pa1")
            assert state["ma-c"] == 1
            stats = await detect_missing_models("pa1", ["ma-a", "ma-b"])  # miss 2
            assert stats["missing_now"] == ["ma-c"]
            assert stats["suggestions_created"] == []  # 无组员不建
            await detect_missing_models("pa1", ["ma-a", "ma-b", "ma-c"])  # 回归
            state = await _absence_snapshot_state("pa1")
            assert state["ma-c"] == 0
        finally:
            await _absence_cleanup(["pa1"], [])
    asyncio.run(run())


def test_absence_suggestion_with_member_and_idempotent():
    """有活跃组员 → 连续缺失生成出组建议; 重复检测幂等只一条"""
    async def run():
        await _ensure_db_async()
        from intelligence.model_absence import detect_missing_models
        from db.engine import async_session_factory
        from db.models import ModelGroupMember, ModelChangeSuggestion
        from sqlalchemy import select
        try:
            async with async_session_factory() as s:
                s.add(ModelGroupMember(group_id="tier:code", model_id="ma-old",
                                       priority=100, weight=1.0, added_by="test"))
                await s.commit()
            await detect_missing_models("pa2", ["ma-old", "ma-new"])
            await detect_missing_models("pa2", ["ma-new"])
            stats = await detect_missing_models("pa2", ["ma-new"])
            assert stats["suggestions_created"] == ["ma-old"]
            await detect_missing_models("pa2", ["ma-new"])
            await detect_missing_models("pa2", ["ma-new"])
            async with async_session_factory() as s:
                rows = list((await s.execute(select(
                    ModelChangeSuggestion.model_id).where(
                    ModelChangeSuggestion.model_id == "ma-old",
                    ModelChangeSuggestion.status == "pending"))).scalars().all())
                assert rows == ["ma-old"]  # 幂等只一条
        finally:
            await _absence_cleanup(["pa2"], ["ma-old"])
    asyncio.run(run())


def test_absence_shrink_guard():
    """列骤降过半: 跳过检测且快照不动"""
    async def run():
        await _ensure_db_async()
        from intelligence.model_absence import detect_missing_models
        try:
            await detect_missing_models("pa3", ["mb-%d" % i for i in range(10)])
            stats = await detect_missing_models("pa3", ["mb-0", "mb-1", "mb-2", "mb-3"])
            assert stats["shrunk_skipped"] is True
            state = await _absence_snapshot_state("pa3")
            assert len(state) == 10
        finally:
            await _absence_cleanup(["pa3"], [])
    asyncio.run(run())


if __name__ == "__main__":
    test_status_fresh_db_setup_not_done()
    test_add_channel_creates_everything_and_flips_setup_done()
    test_add_channel_duplicate_409()
    test_add_channel_unknown_and_hidden_404()
    test_complete_marks_done_even_without_accounts()
    test_test_key_endpoint_validation()
    test_catalog_prices_match_single_source()
    test_seed_groups_idempotent()
    test_group_edit_lifecycle()
    print("all onboarding tests passed")
