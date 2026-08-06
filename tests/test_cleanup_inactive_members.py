"""P1-2 (2026-08-06) — 软删除 cleanup 脚本单元测试

覆盖:
1. DB 缺 P1-2 列 → skip 并返 0 (防御性)
2. 30 天前软删除 → 待清理
3. 30 天内软删除 → 保留 (audit trail)
4. is_active=1 的行 → 永不清理
5. dry_run=True 不真删, dry_run=False 真删
6. 按 removed_reason 分组统计

跑法: python tests/test_cleanup_inactive_members.py
"""

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ============================================================
# Mock config (用 in-memory SQLite)
# ============================================================
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


async def _setup_db_with_p1_2():
    """建 in-memory DB + 跑 P1-2 migration (is_active/removed_at/removed_reason/removed_by)"""
    # 必须在 import 之前改 db_url
    from sqlalchemy.ext.asyncio import create_async_engine
    from db.models import Base
    from sqlalchemy import text

    # 用临时 in-memory engine
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # 模拟 P1-2 migration (create_all 已建表 + 4 列, 因为 ORM 已定义)
        # 但保险起见显式 ALTER 一次 (no-op 因为已存在)
        result = await conn.execute(text("PRAGMA table_info('model_group_members')"))
        cols = {row[1] for row in result.fetchall()}
        if "is_active" not in cols:
            await conn.execute(text(
                "ALTER TABLE model_group_members ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1"
            ))
        if "removed_at" not in cols:
            await conn.execute(text(
                "ALTER TABLE model_group_members ADD COLUMN removed_at DATETIME"
            ))
        if "removed_reason" not in cols:
            await conn.execute(text(
                "ALTER TABLE model_group_members ADD COLUMN removed_reason TEXT"
            ))
        if "removed_by" not in cols:
            await conn.execute(text(
                "ALTER TABLE model_group_members ADD COLUMN removed_by TEXT"
            ))

    # 替换全局 async_session_factory (供 cleanup_mod 使用)
    async_session_factory = lambda: __import__(
        "sqlalchemy.ext.asyncio", fromlist=["AsyncSession"]
    ).AsyncSession(engine, expire_on_commit=False)

    # Patch
    import scripts.cleanup_inactive_members as cleanup_mod
    cleanup_mod.async_session_factory = async_session_factory
    return engine, async_session_factory


async def _insert_test_data(session_factory):
    """插测试数据: 5 行 (3 个待清理 + 2 个保留)"""
    from db.models import ModelGroupMember

    now = datetime.now()
    old = now - timedelta(days=40)         # 40 天前 (应清理)
    recent = now - timedelta(days=10)      # 10 天前 (应保留)

    async with session_factory() as session:
        # 1) 40 天前软删除 + reason=deprecated → 应清理
        m1 = ModelGroupMember(
            group_id="tier:flagship", model_id="old-1",
            is_active=0, removed_at=old, removed_reason="deprecated", removed_by="intel_auto",
        )
        # 2) 40 天前软删除 + reason=tier_change → 应清理
        m2 = ModelGroupMember(
            group_id="tier:balanced", model_id="old-2",
            is_active=0, removed_at=old, removed_reason="tier_change", removed_by="intel_auto",
        )
        # 3) 40 天前软删除 + reason=manual → 应清理
        m3 = ModelGroupMember(
            group_id="tier:light", model_id="old-3",
            is_active=0, removed_at=old, removed_reason="manual", removed_by="manual_approve",
        )
        # 4) 10 天前软删除 (audit trail 内) → 应保留
        m4 = ModelGroupMember(
            group_id="tier:flagship", model_id="recent-1",
            is_active=0, removed_at=recent, removed_reason="deprecated", removed_by="intel_auto",
        )
        # 5) is_active=1 → 永不清理
        m5 = ModelGroupMember(
            group_id="tier:flagship", model_id="active-1",
            is_active=1,
        )
        session.add_all([m1, m2, m3, m4, m5])
        await session.commit()
        return {"to_delete": 3, "to_keep_recent": 1, "to_keep_active": 1}


# ============================================================
# Test 1: DB 缺 P1-2 列 → skip
# ============================================================
def test_schema_check_skip():
    print("\n=== Test 1: DB 缺 P1-2 列 → skip ===")
    # 用一个不含 P1-2 列的临时 in-memory DB
    import subprocess

    # 创建临时 sqlite 文件, 只建 model_group_members 不加 P1-2 列
    test_db = PROJECT_ROOT / "tests" / "_tmp_no_p1_2.db"
    if test_db.exists():
        test_db.unlink()

    # 用 sqlite3 建表 (8 列原始 schema, 不加 P1-2)
    import sqlite3
    conn = sqlite3.connect(test_db)
    conn.executescript("""
        CREATE TABLE model_group_members (
            id INTEGER NOT NULL,
            group_id TEXT NOT NULL,
            model_id TEXT NOT NULL,
            priority INTEGER,
            weight FLOAT,
            max_cost_ratio FLOAT,
            added_by TEXT,
            created_at DATETIME,
            PRIMARY KEY (id)
        );
    """)
    conn.close()

    # 设置 db_path 为这个临时 DB
    import scripts.cleanup_inactive_members as cleanup_mod
    import config as cfg_module

    original_db_url = cfg_module.settings.db_url
    original_db_path = cfg_module.settings.db_path
    cfg_module.settings.db_url = f"sqlite+aiosqlite:///{test_db.as_posix()}"
    cfg_module.settings.db_path = test_db.as_posix()

    try:
        from db.engine import async_session_factory
        cleanup_mod.async_session_factory = async_session_factory

        result = asyncio.run(cleanup_mod.cleanup_inactive_group_members(
            days=30, dry_run=True,
        ))
        assert result["would_delete"] == 0
        assert result["deleted"] == 0
        assert result.get("skipped") == "schema_not_migrated", (
            f"应 skip, 实际 {result}"
        )
        print(f"  [OK] 缺 P1-2 列, skipped={result.get('skipped')}")
    finally:
        cfg_module.settings.db_url = original_db_url
        cfg_module.settings.db_path = original_db_path
        test_db.unlink()


# ============================================================
# Test 2: dry_run 统计正确 (不真删)
# ============================================================
def test_dry_run_count_correct():
    print("\n=== Test 2: dry_run 统计正确 ===")

    async def _setup():
        engine, sf = await _setup_db_with_p1_2()
        counts = await _insert_test_data(sf)
        return sf, counts

    sf, counts = asyncio.run(_setup())

    import scripts.cleanup_inactive_members as cleanup_mod
    cleanup_mod.async_session_factory = sf

    result = asyncio.run(cleanup_mod.cleanup_inactive_group_members(
        days=30, dry_run=True,
    ))
    assert result["would_delete"] == 3, f"应 3 行待清理, 实际 {result['would_delete']}"
    assert result["deleted"] == 0, f"dry_run 不应真删, 实际 deleted={result['deleted']}"
    # 按 reason 分组
    assert result["by_reason"].get("deprecated") == 1
    assert result["by_reason"].get("tier_change") == 1
    assert result["by_reason"].get("manual") == 1
    print(f"  [OK] dry_run: would_delete={result['would_delete']}, "
          f"by_reason={result['by_reason']}")


# ============================================================
# Test 3: 真删 (--apply)
# ============================================================
def test_apply_real_delete():
    print("\n=== Test 3: 真删 ===")

    async def _setup():
        engine, sf = await _setup_db_with_p1_2()
        await _insert_test_data(sf)
        return sf

    sf = asyncio.run(_setup())

    import scripts.cleanup_inactive_members as cleanup_mod
    cleanup_mod.async_session_factory = sf

    # 真删
    result = asyncio.run(cleanup_mod.cleanup_inactive_group_members(
        days=30, dry_run=False,
    ))
    assert result["deleted"] == 3, f"应删 3 行, 实际 {result['deleted']}"

    # 验证: 表里只剩 2 行 (recent-1 + active-1)
    from sqlalchemy import select, func
    from db.models import ModelGroupMember

    async def _count():
        async with sf() as s:
            stmt = select(func.count(ModelGroupMember.id))
            return (await s.execute(stmt)).scalar()

    remaining = asyncio.run(_count())
    assert remaining == 2, f"应剩 2 行, 实际 {remaining}"
    print(f"  [OK] 真删 3 行, 剩余 {remaining} 行")


# ============================================================
# Test 4: 自定义 days=7 阈值
# ============================================================
def test_custom_days_threshold():
    print("\n=== Test 4: 自定义 days=7 阈值 ===")

    async def _setup():
        engine, sf = await _setup_db_with_p1_2()
        await _insert_test_data(sf)
        return sf

    sf = asyncio.run(_setup())

    import scripts.cleanup_inactive_members as cleanup_mod
    cleanup_mod.async_session_factory = sf

    # days=7: recent-1 (10 天前) 也应被清理
    result = asyncio.run(cleanup_mod.cleanup_inactive_group_members(
        days=7, dry_run=True,
    ))
    assert result["would_delete"] == 4, (
        f"应 4 行 (3 个 40 天 + 1 个 10 天), 实际 {result['would_delete']}"
    )
    print(f"  [OK] days=7 阈值: {result['would_delete']} 行 (含 10 天前 recent-1)")


# ============================================================
# Main
# ============================================================
def main():
    print("=" * 60)
    print("P1-2 — 软删除 cleanup 脚本测试")
    print("=" * 60)

    test_schema_check_skip()
    test_dry_run_count_correct()
    test_apply_real_delete()
    test_custom_days_threshold()

    print("\n" + "=" * 60)
    print("ALL 4 TESTS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    main()