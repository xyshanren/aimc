"""intel_auto 出组护栏测试 (2026-10-12)

v4-flash 教训: 9/28 sensenova/taotoken 仍 0 元供应 deepseek-v4-flash,
intel_auto 一步软删其 tier:strong/code 组员行. 护栏后:
- 渠道仍有价行供应的 deprecated 模型 → 不自动出组 (registry 不动/组员不动),
  写 pending capability log + remove_from_group suggestion 待人工确认
- 渠道无供应 → 走原全自动路径 (registry deprecated + 组员软删)

用真实 in-memory sqlite + 真 crud, 不走 test_phase3_3 的 dict mock.

跑法: python -m pytest tests/test_intel_auto_leave_guardrail.py -q
"""

import asyncio
import sys
import types
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _install_mock_config():
    import types

    class _MockSettings:
        openai_api_key = "sk-test"
        openai_base_url = "https://mock.local/v1"
        parse_model = "gpt-4o-mini"
        parse_confidence_threshold = 0.6
        crawl_enabled = False
        aimc_master_key = "0" * 64
        route_refresh_interval = 600
        db_url = "sqlite+aiosqlite:///:memory:"
        db_path = ":memory:"

    sys.modules.setdefault("config", types.ModuleType("config"))
    sys.modules["config"].settings = _MockSettings()


_install_mock_config()


async def _make_session():
    """真实 in-memory sqlite + 全表建库"""
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from db.models import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    return maker()


async def _seed_session(session, *, with_channel: bool, model_id: str):
    """建渠道(+价行) + registry + 两组员行 (group_id 用 TEXT 组名, 同生产)"""
    from db.models import (
        Channel, PriceSnapshot, ModelRegistry,
        ModelGroup, ModelGroupMember,
    )

    ch = Channel(
        id=1, name="sensenova_official", provider="sensenova",
        status="active", priority=5,
        base_url="https://mock.local", api_base="https://mock.local",
    )
    session.add(ch)
    if with_channel:
        session.add(PriceSnapshot(
            channel_id=1, model=model_id, price_type="normal",
            input_price=0.0, output_price=0.0, unit="1M",
        ))
    session.add(ModelRegistry(
        model_id=model_id, family="qwen", display_name=model_id,
        tier="T1", modality="text", status="active",
    ))
    session.add(ModelGroup(
        id=2, group_id="tier:strong", enabled=1, sort_order=2,
    ))
    for gname in ("tier:strong", "tier:code"):
        session.add(ModelGroupMember(
            group_id=gname, model_id=model_id,
            priority=100, weight=1.0, added_by="manual",
            is_active=1,
        ))
    await session.commit()


def _deprecated_item(model_id: str, conf: float = 0.95):
    from schemas.intel import ModelIntelItem
    return ModelIntelItem(
        model_id=model_id, family="qwen", inferred_tier="T1",
        confidence=conf, status="deprecated", confidence_level="high",
    )


def test_guardrail_channel_supplies_blocks_auto_leave():
    """渠道仍供应 → 不自动出组, 降级 suggestion + pending log"""
    async def run():
        session = await _make_session()
        await _seed_session(session, with_channel=True, model_id="guard-model")
        from intelligence.model_sync import process_model_item
        result = await process_model_item(session, _deprecated_item("guard-model"))
        await session.commit()

        from db.models import (
            ModelRegistry, ModelGroupMember,
            ModelCapabilitiesLog, ModelChangeSuggestion,
        )
        from sqlalchemy import select

        assert result.action == "pending_review", f"应 pending_review, 实际 {result.action}"

        reg = (await session.execute(
            select(ModelRegistry).where(ModelRegistry.model_id == "guard-model")
        )).scalar_one()
        assert reg.status == "active", f"registry 不应被标 deprecated, 实际 {reg.status}"

        members = (await session.execute(
            select(ModelGroupMember).where(ModelGroupMember.model_id == "guard-model")
        )).scalars().all()
        assert len(members) == 2
        assert all(m.is_active == 1 for m in members), "组员不应被软删"

        logs = (await session.execute(
            select(ModelCapabilitiesLog).where(
                ModelCapabilitiesLog.model_id == "guard-model",
                ModelCapabilitiesLog.field_changed == "status",
            )
        )).scalars().all()
        assert len(logs) == 1 and logs[0].approved == 0, "应恰好 1 条 pending status log"

        sugg = (await session.execute(
            select(ModelChangeSuggestion).where(
                ModelChangeSuggestion.model_id == "guard-model",
                ModelChangeSuggestion.change_type == "remove_from_group",
            )
        )).scalars().all()
        assert len(sugg) == 1 and sugg[0].status == "pending", "应建 1 条 pending suggestion"
        assert "tier:strong" in sugg[0].current_value, "suggestion 应列出活跃组"
        return session

    asyncio.run(run())
    print("  [OK] 渠道供应 → pending_review, registry/组员不动, log+suggestion 落库")


def test_guardrail_suggestion_idempotent():
    """重复扫同模型 → suggestion 不重复 (幂等)"""
    async def run():
        session = await _make_session()
        await _seed_session(session, with_channel=True, model_id="idem-model")
        from intelligence.model_sync import process_model_item
        await process_model_item(session, _deprecated_item("idem-model"))
        await process_model_item(session, _deprecated_item("idem-model"))
        await session.commit()

        from db.models import ModelChangeSuggestion, ModelCapabilitiesLog
        from sqlalchemy import select, func

        n_sugg = (await session.execute(
            select(func.count(ModelChangeSuggestion.id)).where(
                ModelChangeSuggestion.model_id == "idem-model"
            )
        )).scalar()
        assert n_sugg == 1, f"suggestion 应幂等 1 条, 实际 {n_sugg}"

        n_logs = (await session.execute(
            select(func.count(ModelCapabilitiesLog.id)).where(
                ModelCapabilitiesLog.model_id == "idem-model",
                ModelCapabilitiesLog.field_changed == "status",
            )
        )).scalar()
        assert n_logs == 1, f"pending log 应 upsert 幂等 1 条, 实际 {n_logs}"

    asyncio.run(run())
    print("  [OK] 重复扫 suggestion/log 幂等不堆积")


def test_no_channel_supplies_full_auto_leave():
    """渠道无供应 → 原全自动路径: registry deprecated + 组员软删"""
    async def run():
        session = await _make_session()
        await _seed_session(session, with_channel=False, model_id="dead-model")
        from intelligence.model_sync import process_model_item
        result = await process_model_item(session, _deprecated_item("dead-model"))
        await session.commit()

        from db.models import ModelRegistry, ModelGroupMember, ModelChangeSuggestion
        from sqlalchemy import select

        assert result.action == "auto_left", f"应 auto_left, 实际 {result.action}"

        reg = (await session.execute(
            select(ModelRegistry).where(ModelRegistry.model_id == "dead-model")
        )).scalar_one()
        assert reg.status == "deprecated"

        members = (await session.execute(
            select(ModelGroupMember).where(ModelGroupMember.model_id == "dead-model")
        )).scalars().all()
        assert len(members) == 2
        assert all(m.is_active == 0 for m in members)
        assert all(m.removed_by == "intel_auto" for m in members)
        assert all(m.removed_reason == "deprecated+conf>=0.9" for m in members)

        sugg = (await session.execute(
            select(ModelChangeSuggestion).where(
                ModelChangeSuggestion.model_id == "dead-model"
            )
        )).scalars().all()
        assert sugg == [], "全自动路径不应建 suggestion"

    asyncio.run(run())
    print("  [OK] 渠道无供应 → 全自动出组 (registry deprecated + 组员软删)")


if __name__ == "__main__":
    test_guardrail_channel_supplies_blocks_auto_leave()
    test_guardrail_suggestion_idempotent()
    test_no_channel_supplies_full_auto_leave()
    print("3 tests passed")
