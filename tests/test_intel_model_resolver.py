"""2026-11-04 (O1) — intel 模型名解析 + 供给侧闸门 + 待确认队列行为

覆盖:
1. 种子数据 (registry + 价行 raw_model_id + 上游快照 + 已批准别名日志)
2. resolve_canonical_model 六步全部命中路径 + 未命中
3. is_suppliable_by_any_channel 四类供给证据 + 无证据
4. process_model_item:
   - 无供应证据 → action=unsupplied, 不写 capabilities_log (噪声根治)
   - 解析命中现网模型 (高置信) → action=resolved, 不写 capabilities_log
   - 启发式解析 (suffix_strip) → 写 pending 且 new_value 带 resolved_model_id
   - 别名记忆: 批准过的解析下次直接高置信命中
5. pricing_writer: 解析后写规范名 + raw_model_id (O1 小修)

跑法: python -m pytest tests/test_intel_model_resolver.py -q
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


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


# ============================================================
# 真 in-memory DB 基建 (StaticPool: 跨 session 同一连接)
# ============================================================
_ENGINE = None
_FACTORY = None


async def _ensure_factory():
    """懒建 in-memory engine (在已有 event loop 内执行, 不能 asyncio.run)"""
    global _ENGINE, _FACTORY
    if _FACTORY is not None:
        return _FACTORY
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool

    from db.models import Base

    _ENGINE = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with _ENGINE.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    _FACTORY = async_sessionmaker(_ENGINE, expire_on_commit=False)
    return _FACTORY


@pytest.fixture()
async def db_session():
    """每用例独立 in-memory 库 (StaticPool 共享连接, 复用会跨用例污染)"""
    import warnings

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool

    from db.models import Base

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        await engine.dispose()


async def _seed_registry(session, model_id, *, display_name=None, tier="T2",
                         modality="text", context_length=None):
    from db import crud
    await crud.upsert_model_registry(
        session, model_id=model_id, family=model_id.split("-")[0],
        tier=tier, display_name=display_name, modality=modality,
        context_length=context_length,
    )
    await session.flush()


async def _seed_channel_and_price(session, provider, model, *, raw_model_id=None):
    from db import crud
    ch = await crud.create_channel(
        session, name=f"test-{provider}", provider=provider,
        base_url="https://mock.local/v1", status="active",
    )
    await session.flush()
    await crud.create_price_snapshot(
        session, channel_id=ch.id, model=model, price_type="normal",
        input_price=0.001, output_price=0.002,
        raw_model_id=raw_model_id,
    )
    await session.flush()
    return ch.id


async def _seed_snapshot(session, provider, model):
    from db.models import ModelListSnapshot
    session.add(ModelListSnapshot(provider=provider, model=model))
    await session.flush()


# ============================================================
# 1. resolver 各步
# ============================================================
async def test_resolver_exact_and_normalized(db_session):
    from intelligence.model_resolver import resolve_canonical_model
    await _seed_registry(db_session, "deepseek-flash", display_name="DeepSeek V4.1 Flash")

    r = await resolve_canonical_model(db_session, "deepseek-flash")
    assert (r.canonical, r.method, r.confidence) == ("deepseek-flash", "exact", "high")

    r = await resolve_canonical_model(db_session, "  DeepSeek-Flash  ")
    assert (r.canonical, r.method) == ("deepseek-flash", "normalized")


async def test_resolver_raw_model_id_reverse(db_session):
    from intelligence.model_resolver import resolve_canonical_model
    await _seed_registry(db_session, "deepseek-flash")
    await _seed_channel_and_price(
        db_session, "modelscope", "deepseek-flash",
        raw_model_id="deepseek-ai/DeepSeek-V4.1-Flash-1017",
    )

    # 展示名形态 → 经 raw_model_id 反查命中
    r = await resolve_canonical_model(db_session, "DeepSeek-V4.1-Flash")
    assert (r.canonical, r.method, r.confidence) == ("deepseek-flash", "raw_model_id", "high")


async def test_resolver_suffix_strip_proposal(db_session):
    from intelligence.model_resolver import resolve_canonical_model
    await _seed_registry(db_session, "glm-5.3")

    r = await resolve_canonical_model(db_session, "glm-5.3-preview")
    assert (r.canonical, r.method, r.confidence) == ("glm-5.3", "suffix_strip", "proposal")
    assert r.is_proposal


async def test_resolver_display_name(db_session):
    from intelligence.model_resolver import resolve_canonical_model
    await _seed_registry(db_session, "minimax-m3", display_name="MiniMax M3")

    r = await resolve_canonical_model(db_session, "M3", display_name="MiniMax M3")
    assert (r.canonical, r.method) == ("minimax-m3", "display_name")


async def test_resolver_alias_log_memory(db_session):
    from intelligence import model_sync
    from db.models import ModelCapabilitiesLog
    from intelligence.model_resolver import resolve_canonical_model
    await _seed_registry(db_session, "step-1o")
    # 模拟: 之前人工批准过一条 metadata, 里面带 resolved_model_id
    db_session.add(ModelCapabilitiesLog(
        model_id="Step 1o (升级版)", field_changed="metadata",
        old_value=None,
        new_value=json.dumps({
            "model_id": "Step 1o (升级版)",
            "resolved_model_id": "step-1o",
            "resolved_by": "display_name",
        }, ensure_ascii=False),
        source="ai_infer", confidence=0.9, approved=1, provider="stepfun",
    ))
    await db_session.flush()

    r = await resolve_canonical_model(db_session, "Step 1o (升级版)")
    assert (r.canonical, r.method, r.confidence) == ("step-1o", "alias_log", "high")


async def test_resolver_unresolved(db_session):
    from intelligence.model_resolver import resolve_canonical_model
    r = await resolve_canonical_model(db_session, "Doubao Seedream 5.0 flash")
    assert not r.resolved


# ============================================================
# 2. 供给判定
# ============================================================
async def test_suppliable_via_price_row(db_session):
    from intelligence.model_resolver import is_suppliable_by_any_channel
    await _seed_registry(db_session, "deepseek-flash")
    await _seed_channel_and_price(db_session, "deepseek", "deepseek-flash")

    ok, reason = await is_suppliable_by_any_channel(
        db_session, raw_model_id="deepseek-flash", canonical="deepseek-flash",
    )
    assert (ok, reason) == (True, "price_row")


async def test_suppliable_via_raw_model_id(db_session):
    from intelligence.model_resolver import is_suppliable_by_any_channel
    await _seed_channel_and_price(
        db_session, "modelscope", "deepseek-flash",
        raw_model_id="deepseek-ai/DeepSeek-V4.1-Flash-1017",
    )
    ok, reason = await is_suppliable_by_any_channel(
        db_session, raw_model_id="DeepSeek-V4.1-Flash",
    )
    assert (ok, reason) == (True, "price_raw_id")


async def test_suppliable_via_upstream_list(db_session):
    from intelligence.model_resolver import is_suppliable_by_any_channel
    await _seed_snapshot(db_session, "siliconflow", "deepseek-ai/DeepSeek-V4.1-Flash")
    ok, reason = await is_suppliable_by_any_channel(
        db_session, raw_model_id="DeepSeek-V4.1-Flash", source_provider="siliconflow",
    )
    assert (ok, reason) == (True, "upstream_list")


async def test_suppliable_via_channel_exists(db_session):
    from intelligence.model_resolver import is_suppliable_by_any_channel
    await _seed_channel_and_price(db_session, "sensenova", "sensenova-other")  # 建渠道
    ok, reason = await is_suppliable_by_any_channel(
        db_session, raw_model_id="sensenova-new-model", source_provider="sensenova",
    )
    assert (ok, reason) == (True, "channel_exists")


async def test_unsuppliable(db_session):
    from intelligence.model_resolver import is_suppliable_by_any_channel
    ok, reason = await is_suppliable_by_any_channel(
        db_session, raw_model_id="Doubao Seedream 5.0 flash", source_provider="volcengine",
    )
    assert (ok, reason) == (False, None)


# ============================================================
# 3. process_model_item 队列行为
# ============================================================
def _make_item(model_id, *, conf=0.7, tier=None, family="doubao",
               modality="text", provider="volcengine", display_name=None):
    from schemas.intel import ModelIntelItem
    return ModelIntelItem(
        model_id=model_id, family=family, display_name=display_name,
        inferred_tier=tier, capabilities=["general"], context_length=None,
        modality=modality, is_open_source=None, status="active",
        confidence=conf, confidence_level="high" if conf >= 0.8 else "medium",
        tier_reason="test", pricing=[], source_provider=provider,
    )


async def _process(db_session, item):
    import intelligence.model_sync as model_sync
    return await model_sync.process_model_item(db_session, item)


async def _pending_logs(session):
    from db.models import ModelCapabilitiesLog
    from sqlalchemy import select
    rows = (await session.execute(
        select(ModelCapabilitiesLog).where(ModelCapabilitiesLog.approved == 0)
    )).scalars().all()
    return rows


async def test_unsupplied_skips_pending(db_session):
    item = _make_item("Doubao Seedream 5.0 flash")
    result = await _process(db_session, item)
    assert result.action == "unsupplied", result
    logs = await _pending_logs(db_session)
    assert logs == [], "无供应证据的模型不应写 capabilities_log"


async def test_resolved_high_confidence_no_pending(db_session):
    from intelligence.model_resolver import resolve_canonical_model
    await _seed_registry(db_session, "deepseek-flash", modality="multimodal")
    await _seed_channel_and_price(
        db_session, "modelscope", "deepseek-flash",
        raw_model_id="deepseek-ai/DeepSeek-V4.1-Flash-1017",
    )

    item = _make_item(
        "DeepSeek-V4.1-Flash", family="deepseek", modality="text",
        provider="modelscope", display_name="DeepSeek-V4.1-Flash（升级 1017）",
    )
    result = await _process(db_session, item)
    assert result.action == "resolved", result
    assert result.target_group == "deepseek-flash"
    logs = await _pending_logs(db_session)
    assert logs == [], "解析命中现网模型不应写 pending"

    # 真冲突只留应用日志, 不改 registry
    from db import crud
    reg = await crud.get_model_registry(db_session, "deepseek-flash")
    assert reg.modality == "multimodal", "registry 不被 intel 覆盖"


async def test_proposal_resolution_writes_pending_with_resolved_id(db_session):
    await _seed_registry(db_session, "glm-5.3")
    await _seed_channel_and_price(db_session, "glm", "glm-4.5")  # 供给证据 (渠道路)
    item = _make_item("glm-5.3-preview", family="glm", provider="glm")
    result = await _process(db_session, item)
    assert result.action == "pending_review", result

    logs = await _pending_logs(db_session)
    assert len(logs) == 1
    payload = json.loads(logs[0].new_value)
    assert payload["resolved_model_id"] == "glm-5.3"
    assert payload["resolved_by"] == "suffix_strip"
    assert payload["resolution"] == "proposal"


async def test_alias_log_short_circuits_next_scan(db_session):
    """启发式解析被批准后, 下次扫描直接高置信命中 → 不再产生 pending"""
    from db.models import ModelCapabilitiesLog
    from intelligence.model_resolver import resolve_canonical_model
    await _seed_registry(db_session, "step-1o")
    await _seed_channel_and_price(db_session, "stepfun", "step-2")  # 供给证据

    item = _make_item("step-1o-preview", family="step", provider="stepfun")
    result = await _process(db_session, item)
    assert result.action == "pending_review", result

    # 人工批准 (approve 语义: metadata 行只结案, 但解析结果留在了 new_value)
    rows = await _pending_logs(db_session)
    rows[0].approved = 1
    await db_session.flush()

    # 下次扫描
    result2 = await _process(db_session, item)
    assert result2.action == "resolved", result2
    r = await resolve_canonical_model(db_session, "step-1o-preview")
    assert r.method == "alias_log"


# ============================================================
# 4. pricing_writer: 规范名 + raw_model_id
# ============================================================
async def test_pricing_writer_writes_canonical_plus_raw(db_session, monkeypatch):
    from schemas.intel import ModelIntelItem, ModelPricingItem
    from intelligence.pricing_writer import _upsert_pricing_for_model
    await _seed_registry(db_session, "deepseek-flash")
    await _seed_channel_and_price(
        db_session, "modelscope", "deepseek-flash",
        raw_model_id="deepseek-ai/DeepSeek-V4.1-Flash-1017",
    )
    ch = await _seed_channel_and_price(db_session, "deepseek", "deepseek-official")
    # 路由缓存刷新走真实 route_engine 太重, 改 patch 掉 _invalidate_channel_price
    import intelligence.pricing_writer as pw
    calls = []

    async def _fake_invalidate(channel_id):
        calls.append(channel_id)
        return True

    monkeypatch.setattr(pw, "_invalidate_channel_price", _fake_invalidate)

    item = ModelIntelItem(
        model_id="DeepSeek-V4.1-Flash", family="deepseek",
        display_name="DeepSeek-V4.1-Flash", inferred_tier=None,
        capabilities=["general"], context_length=None, modality="text",
        is_open_source=None, status="active", confidence=0.9,
        confidence_level="high", tier_reason="test",
        pricing=[ModelPricingItem(type="normal", input_price=1.0, output_price=2.0)],
        source_provider="modelscope",
    )

    from db.models import PriceSnapshot
    from sqlalchemy import select
    async with db_session.begin_nested():
        synced, refreshed = await _upsert_pricing_for_model(db_session, ch, item)
    assert synced == 1
    assert refreshed == 1, "写价后应精准刷该渠道路由缓存"

    rows = (await db_session.execute(
        select(PriceSnapshot).where(PriceSnapshot.model == "deepseek-flash")
    )).scalars().all()
    assert rows, "应存在规范名价行"
    assert any(r.raw_model_id == "DeepSeek-V4.1-Flash" for r in rows), (
        "规范名行必须带上游线名"
    )
    assert calls == [ch]


async def test_pricing_writer_skips_unresolved_wire_name(db_session):
    from schemas.intel import ModelIntelItem, ModelPricingItem
    from intelligence.pricing_writer import _upsert_pricing_for_model
    ch = await _seed_channel_and_price(db_session, "deepseek", "deepseek-flash")

    item = ModelIntelItem(
        model_id="someone-org/Some-Brand-New-Model-123", family="deepseek",
        display_name=None, inferred_tier=None, capabilities=["general"],
        context_length=None, modality="text", is_open_source=None,
        status="active", confidence=0.8, confidence_level="high",
        tier_reason="test",
        pricing=[ModelPricingItem(type="normal", input_price=1.0, output_price=2.0)],
        source_provider="deepseek",
    )
    from db.models import PriceSnapshot
    from sqlalchemy import select
    prior = len((await db_session.execute(select(PriceSnapshot))).scalars().all())
    synced, _refreshed = await _upsert_pricing_for_model(db_session, ch, item)
    assert synced == 0, "未解析的线名不应污染 canonical 键空间"
    after = len((await db_session.execute(select(PriceSnapshot))).scalars().all())
    assert after == prior


if __name__ == "__main__":
    import warnings
    warnings.filterwarnings("ignore")
    import pytest as _pytest
    sys.exit(_pytest.main([__file__, "-q"]))