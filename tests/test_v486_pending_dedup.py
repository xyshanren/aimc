"""V4.8.6 — pending log 去重测试

验证:
1. 同 (model_id, field_changed) approved=0 的 log 写两次, 只保留 1 条 (upsert)
2. upsert 后 new_value / confidence 被更新
3. approved=1 的行不受去重约束 (可以有多条历史)

跑法: python tests/test_v486_pending_dedup.py
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
        aimc_master_key = "0" * 64
        route_refresh_interval = 600
        db_url = "sqlite+aiosqlite:///:memory:"
        db_path = "./data/aimc.db"

    sys.modules.setdefault("config", types.ModuleType("config"))
    sys.modules["config"].settings = _MockSettings()


_install_mock_config()


async def _setup_db():
    """初始化 in-memory DB, 跑 ensure_migrations"""
    from db.engine import engine
    from db.init_db import init_db
    await init_db()
    return engine


async def _test_pending_dedup_upsert():
    """Test 1: 同 model+field approved=0 写两次 → 只 1 条, new_value 被更新"""
    print("\n=== Test 1: pending 行 upsert 去重 ===")
    await _setup_db()
    from db.engine import async_session_factory
    from db import crud
    from sqlalchemy import select
    from db.models import ModelCapabilitiesLog

    async with async_session_factory() as session:
        # 第一次写
        await crud.log_capability_change(
            session, model_id="test-dedup-1", field_changed="tier",
            old_value="T2", new_value="T1",
            source="ai_infer", confidence=0.85, approved=0,
            provider="deepseek",
        )
        await session.commit()

        # 第二次写同 model+field (approved=0) → 应 upsert 更新, 不新增
        await crud.log_capability_change(
            session, model_id="test-dedup-1", field_changed="tier",
            old_value="T2", new_value="T0",
            source="ai_infer", confidence=0.95, approved=0,
            provider="deepseek",
        )
        await session.commit()

        # 查询 approved=0 的行
        stmt = select(ModelCapabilitiesLog).where(
            ModelCapabilitiesLog.model_id == "test-dedup-1",
            ModelCapabilitiesLog.field_changed == "tier",
            ModelCapabilitiesLog.approved == 0,
        )
        rows = (await session.execute(stmt)).scalars().all()
        assert len(rows) == 1, (
            f"同 model+field approved=0 应只 1 条 (upsert), 实际 {len(rows)}"
        )
        row = rows[0]
        assert row.new_value == "T0", (
            f"new_value 应被更新为 T0, 实际 {row.new_value}"
        )
        assert row.confidence == 0.95, (
            f"confidence 应被更新为 0.95, 实际 {row.confidence}"
        )
        assert row.provider == "deepseek"
    print("  [OK] pending 行 upsert: 只 1 条, new_value=T0, conf=0.95")


async def _test_approved_not_deduped():
    """Test 2: approved=1 的行不受去重约束"""
    print("\n=== Test 2: approved=1 行不受去重约束 ===")
    await _setup_db()
    from db.engine import async_session_factory
    from db import crud
    from sqlalchemy import select
    from db.models import ModelCapabilitiesLog

    async with async_session_factory() as session:
        # 写两条 approved=1 的同 model+field → 应都保留
        await crud.log_capability_change(
            session, model_id="test-history", field_changed="tier",
            old_value="T2", new_value="T1",
            source="manual", confidence=None, approved=1,
        )
        await session.commit()
        await crud.log_capability_change(
            session, model_id="test-history", field_changed="tier",
            old_value="T1", new_value="T0",
            source="manual", confidence=None, approved=1,
        )
        await session.commit()

        stmt = select(ModelCapabilitiesLog).where(
            ModelCapabilitiesLog.model_id == "test-history",
            ModelCapabilitiesLog.field_changed == "tier",
            ModelCapabilitiesLog.approved == 1,
        ).order_by(ModelCapabilitiesLog.id)
        rows = (await session.execute(stmt)).scalars().all()
        assert len(rows) == 2, (
            f"approved=1 应保留历史 (2 条), 实际 {len(rows)}"
        )
        assert rows[0].new_value == "T1"
        assert rows[1].new_value == "T0"
    print("  [OK] approved=1 保留 2 条历史")


async def _test_different_fields_not_deduped():
    """Test 3: 不同 field_changed 的 pending 行各自独立"""
    print("\n=== Test 3: 不同 field 各自独立 ===")
    await _setup_db()
    from db.engine import async_session_factory
    from db import crud
    from sqlalchemy import select
    from db.models import ModelCapabilitiesLog

    async with async_session_factory() as session:
        await crud.log_capability_change(
            session, model_id="test-multi", field_changed="tier",
            old_value=None, new_value="T1",
            confidence=0.8, approved=0,
        )
        await crud.log_capability_change(
            session, model_id="test-multi", field_changed="context_length",
            old_value=32768, new_value=64000,
            confidence=0.8, approved=0,
        )
        await session.commit()

        stmt = select(ModelCapabilitiesLog).where(
            ModelCapabilitiesLog.model_id == "test-multi",
            ModelCapabilitiesLog.approved == 0,
        )
        rows = (await session.execute(stmt)).scalars().all()
        assert len(rows) == 2, (
            f"不同 field 应各自 1 条 (共 2 条), 实际 {len(rows)}"
        )
    print("  [OK] 不同 field 各自独立, 共 2 条 pending")


async def _test_approve_then_new_pending():
    """Test 4: 批准后再写新的 pending → 可以新建 (不再被旧 pending upsert)"""
    print("\n=== Test 4: 批准后可写新 pending ===")
    await _setup_db()
    from db.engine import async_session_factory
    from db import crud
    from sqlalchemy import select
    from db.models import ModelCapabilitiesLog

    async with async_session_factory() as session:
        # 写一条 pending
        await crud.log_capability_change(
            session, model_id="test-approve", field_changed="tier",
            old_value="T2", new_value="T1",
            confidence=0.8, approved=0,
        )
        await session.commit()

        # 查到那条 pending 的 id, 批准它
        stmt = select(ModelCapabilitiesLog).where(
            ModelCapabilitiesLog.model_id == "test-approve",
            ModelCapabilitiesLog.approved == 0,
        )
        row = (await session.execute(stmt)).scalar_one()
        row.approved = 1
        await session.commit()

        # 再写一条新 pending → 应该新建 (因为旧 pending 已 approved=1)
        await crud.log_capability_change(
            session, model_id="test-approve", field_changed="tier",
            old_value="T1", new_value="T0",
            confidence=0.9, approved=0,
        )
        await session.commit()

        # 应该有 1 条 approved=1 + 1 条 approved=0
        stmt0 = select(ModelCapabilitiesLog).where(
            ModelCapabilitiesLog.model_id == "test-approve",
            ModelCapabilitiesLog.approved == 0,
        )
        stmt1 = select(ModelCapabilitiesLog).where(
            ModelCapabilitiesLog.model_id == "test-approve",
            ModelCapabilitiesLog.approved == 1,
        )
        pending = (await session.execute(stmt0)).scalars().all()
        approved = (await session.execute(stmt1)).scalars().all()
        assert len(pending) == 1, f"应有 1 条新 pending, 实际 {len(pending)}"
        assert len(approved) == 1, f"应有 1 条 approved, 实际 {len(approved)}"
        assert pending[0].new_value == "T0"
        assert approved[0].new_value == "T1"
    print("  [OK] 批准后写新 pending 成功, 旧 approved 行保留")


def main():
    asyncio.run(_test_pending_dedup_upsert())
    asyncio.run(_test_approved_not_deduped())
    asyncio.run(_test_different_fields_not_deduped())
    asyncio.run(_test_approve_then_new_pending())
    print("\n✅ V4.8.6 pending dedup 全部测试通过")


if __name__ == "__main__":
    main()
