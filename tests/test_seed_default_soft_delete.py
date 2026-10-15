"""seed_default 软删成员跳过 + deprecated 不入组 回归测试 (2026-10-15)

原 bug: ensure_registry_and_members 的成员补种只查 is_active=1 判重, 遇到
软删行 (is_active=0, 占着 UNIQUE(group_id, model_id) 槽位) 直接 INSERT →
IntegrityError → 看板「初始化默认模型组」按钮 500. 修复: 软删行视为策展
移出 — 跳过不复活也不重建 (复活只走看板手动加成员的显式意图).

同时覆盖: catalog status=deprecated 的模型 (deepseek-v4-flash) 种子时建
registry 行但不入任何组 (对齐生产策展形态).

真实 in-memory sqlite + 真 ORM.
跑法: python -m pytest tests/test_seed_default_soft_delete.py -q
"""

import asyncio
import sys
import types
from datetime import datetime
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


def test_seed_skips_soft_deleted_member():
    """软删成员行 = 策展移出: 补种跳过该组 (不复活不重建, 不撞 UNIQUE)"""
    from sqlalchemy import select
    from db.models import ModelGroupMember
    from db.seed_default import ensure_default_groups, ensure_registry_and_members

    async def run():
        session = await _make_session()
        await ensure_default_groups(session)
        # 预置: glm-5.2 在 tier:strong 的软删行 (模拟策展移出)
        session.add(ModelGroupMember(
            group_id="tier:strong", model_id="glm-5.2",
            priority=105, is_active=0,
            removed_at=datetime.now(), removed_reason="curated", removed_by="test",
        ))
        await session.commit()

        cfg = {"name": "test_ch", "prices": [("glm-5.2", "normal", "全天", 1.0, 2.0)]}
        stats = await ensure_registry_and_members(session, cfg)
        await session.commit()
        return session, stats

    session, stats = asyncio.run(run())

    async def fetch():
        from sqlalchemy import select
        from db.models import ModelGroupMember
        rows = (await session.execute(
            select(ModelGroupMember).where(
                ModelGroupMember.group_id == "tier:strong",
                ModelGroupMember.model_id == "glm-5.2")
        )).scalars().all()
        code_rows = (await session.execute(
            select(ModelGroupMember).where(
                ModelGroupMember.group_id == "tier:code",
                ModelGroupMember.model_id == "glm-5.2")
        )).scalars().all()
        return rows, code_rows

    strong_rows, code_rows = asyncio.run(fetch())
    # glm-5.2 = T1 text + code cap → strong(软删跳过) + code(新插入)
    assert stats["members_created"] == 1
    assert len(strong_rows) == 1, "UNIQUE 槽位复用, 不应出现第二行"
    assert strong_rows[0].is_active == 0, "策展软删行不得被种子复活"
    assert strong_rows[0].removed_reason == "curated"
    assert len(code_rows) == 1 and code_rows[0].is_active == 1


def test_seed_deprecated_not_grouped():
    """status=deprecated 的模型 (deepseek-v4-flash): 建 registry 行, 不入组"""
    from sqlalchemy import select
    from db.models import ModelGroupMember, ModelRegistry
    from db.seed_default import ensure_registry_and_members

    async def run():
        session = await _make_session()
        cfg = {"name": "test_ch", "prices": [("deepseek-v4-flash", "normal", "全天", 0.0, 0.0)]}
        stats = await ensure_registry_and_members(session, cfg)
        await session.commit()
        return session, stats

    session, stats = asyncio.run(run())

    async def fetch():
        reg = (await session.execute(
            select(ModelRegistry).where(ModelRegistry.model_id == "deepseek-v4-flash")
        )).scalars().first()
        members = (await session.execute(
            select(ModelGroupMember).where(ModelGroupMember.model_id == "deepseek-v4-flash")
        )).scalars().all()
        return reg, members

    reg, members = asyncio.run(fetch())
    assert stats["registry_created"] == 1
    assert reg is not None and reg.status == "deprecated"
    assert len(members) == 0, "deprecated 模型不应入任何组"


def test_seed_rerun_zero_diff():
    """幂等: 重跑零 diff (已有 active 成员不重复建)"""
    from db.seed_default import ensure_registry_and_members

    async def run():
        session = await _make_session()
        cfg = {"name": "test_ch", "prices": [("glm-5.2", "normal", "全天", 1.0, 2.0)]}
        first = await ensure_registry_and_members(session, cfg)
        await session.commit()
        second = await ensure_registry_and_members(session, cfg)
        await session.commit()
        return first, second

    first, second = asyncio.run(run())
    assert first["members_created"] >= 1
    assert second["members_created"] == 0
    assert second["registry_created"] == 0


if __name__ == "__main__":
    test_seed_skips_soft_deleted_member()
    test_seed_deprecated_not_grouped()
    test_seed_rerun_zero_diff()
    print("all seed_default soft-delete tests passed")
