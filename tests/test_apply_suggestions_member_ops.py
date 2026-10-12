"""apply_suggestions 组员操作 join 修复测试 (2026-10-12)

原 bug: member.group_id 存 TEXT 组名, apply 用 ModelGroupMember.group_id == g.id
(整型) 比较 → remove 永不命中 (批准后静默 SKIP), add 存在性检查失效且新行插成
整型 id (路由 TEXT 匹配看不见). 修复后统一用 g.group_id (TEXT).

真实 in-memory sqlite + 真 ORM, 覆盖: remove_from_group / add_to_group 的
apply=True 生效 + dry-run(apply=False) 不动库.

跑法: python -m pytest tests/test_apply_suggestions_member_ops.py -q
"""

import asyncio
import sys
import types
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _install_mock_config():
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
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from db.models import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)()


async def _seed(session):
    from db.models import ModelGroup, ModelGroupMember

    session.add(ModelGroup(id=2, group_id="tier:strong", enabled=1, sort_order=2))
    session.add(ModelGroupMember(
        group_id="tier:strong", model_id="glm-5.2",
        priority=108, weight=1.0, added_by="manual", is_active=1,
    ))
    await session.commit()


def test_remove_from_group_applies():
    """remove_from_group apply=True 应真软删 TEXT 组名行 (修复前 SKIP)"""
    async def run():
        session = await _make_session()
        await _seed(session)
        from db.models import ModelChangeSuggestion
        from scripts.apply_suggestions import apply_model_suggestion

        s = ModelChangeSuggestion(
            model_id="glm-5.2", change_type="remove_from_group",
            suggested_value="tier:strong", status="approved", confidence=0.95,
        )
        msg = await apply_model_suggestion(session, s, apply=True)
        await session.commit()

        assert "SKIP" not in msg, f"修复后不应 SKIP: {msg}"
        from sqlalchemy import select
        from db.models import ModelGroupMember
        m = (await session.execute(
            select(ModelGroupMember).where(
                ModelGroupMember.group_id == "tier:strong",
                ModelGroupMember.model_id == "glm-5.2",
            )
        )).scalar_one()
        assert m.is_active == 0, "apply 后组员应软删"
        assert m.removed_by == "apply_suggestions.py"

    asyncio.run(run())
    print("  [OK] remove_from_group apply=True 真软删 TEXT 组名行")


def test_add_to_group_applies():
    """add_to_group apply=True 新行 group_id 应为 TEXT 组名 (修复前插整型 id)"""
    async def run():
        session = await _make_session()
        await _seed(session)
        from db.models import ModelChangeSuggestion
        from scripts.apply_suggestions import apply_model_suggestion

        s = ModelChangeSuggestion(
            model_id="kimi-k3", change_type="add_to_group",
            suggested_value="tier:strong", status="approved", confidence=0.95,
        )
        msg = await apply_model_suggestion(session, s, apply=True)
        await session.commit()

        assert "SKIP" not in msg, f"修复后不应 SKIP: {msg}"
        from sqlalchemy import select
        from db.models import ModelGroupMember
        m = (await session.execute(
            select(ModelGroupMember).where(ModelGroupMember.model_id == "kimi-k3")
        )).scalar_one()
        assert m.group_id == "tier:strong", (
            f"新行 group_id 应为 TEXT 组名, 实际 {m.group_id!r} (整型 id=修复失效)"
        )
        assert m.is_active == 1

    asyncio.run(run())
    print("  [OK] add_to_group apply=True 新行 group_id=TEXT 组名")


def test_dry_run_no_mutation():
    """apply=False 只返回描述不动库"""
    async def run():
        session = await _make_session()
        await _seed(session)
        from db.models import ModelChangeSuggestion
        from scripts.apply_suggestions import apply_model_suggestion
        from sqlalchemy import select, func
        from db.models import ModelGroupMember

        s = ModelChangeSuggestion(
            model_id="glm-5.2", change_type="remove_from_group",
            suggested_value="tier:strong", status="approved", confidence=0.95,
        )
        await apply_model_suggestion(session, s, apply=False)

        n_active = (await session.execute(
            select(func.count(ModelGroupMember.id)).where(
                ModelGroupMember.is_active == 1
            )
        )).scalar()
        assert n_active == 1, "dry-run 不应动库"

    asyncio.run(run())
    print("  [OK] apply=False dry-run 不动库")


if __name__ == "__main__":
    test_remove_from_group_applies()
    test_add_to_group_applies()
    test_dry_run_no_mutation()
    print("3 tests passed")
