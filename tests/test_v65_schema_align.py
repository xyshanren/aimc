"""V6.5+: 迁移机制收敛轻方案 A 回归.

- schema_align._ALIGN_DDL 与生成器同源 (纯字面量, 数量>0)
- ensure_schema_alignment: 全新库 (create_all 后) 幂等零补列
- ensure_schema_alignment: 人为删列的老库 → 补齐 + 版本表登记 orm-align
- mark_legacy_migrations: 23 个历史脚本登记, 幂等重复跑不再新增
- init_db 全链路: 老库缺列 → init_db 一次跑齐 + 版本表就位
- plan_alignment 纯函数: 全齐库 pending=0, 空库 pending=全部
"""

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from db import schema_align
from db.models import Base


# ============================================================
# 常量同源性
# ============================================================

def test_align_ddl_constant_shape():
    assert len(schema_align._ALIGN_DDL) > 200
    for stmt, (table, col, insert) in schema_align._ALIGN_DDL.items():
        assert stmt.startswith("ALTER TABLE %s ADD COLUMN %s " % (table, col))
        assert "align:%s.%s" % (table, col) in insert
        assert "'orm-align'" in insert
    # 无 f-string/% 拼接痕迹 (Mimosa 过审形态: 纯字面量)
    assert "{" not in next(iter(schema_align._ALIGN_DDL))
    # 主键/NOT NULL 无默认列不进常量 (SQLite ALTER 硬限制)
    assert not any(c == "api_key" for _t, c, _i in schema_align._ALIGN_DDL.values())


def test_plan_alignment_pure_function():
    full = {}
    for stmt, (t, c, _i) in schema_align._ALIGN_DDL.items():
        full.setdefault(t, set()).add(c)
    assert schema_align.plan_alignment(full) == []
    assert len(schema_align.plan_alignment({})) == len(schema_align._ALIGN_DDL)


# ============================================================
# ensure_schema_alignment: 幂等 + 缺列补齐
# ============================================================

@pytest.fixture
async def mem_engine():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


async def test_alignment_idempotent_on_fresh_db(mem_engine):
    """create_all 建的全新库: 比对应零待补列。"""
    sa = schema_align
    async with mem_engine.begin() as conn:
        await conn.exec_driver_sql(sa._VERSION_TABLE_DDL)
        rows = (await conn.exec_driver_sql(sa._ALL_COLUMNS_SQL)).fetchall()
        cols = {}
        for tbl, col in rows:
            cols.setdefault(tbl, set()).add(col)
        pending = sa.plan_alignment(cols)
    assert pending == [], "create_all 建的全新库不应有待补列"


async def test_alignment_fills_missing_column(mem_engine, monkeypatch):
    """模拟老库: 删掉 channels.api_style → alignment 应补回并登记版本表。"""
    sa = schema_align
    async with mem_engine.begin() as conn:
        await conn.exec_driver_sql(sa._VERSION_TABLE_DDL)
        await conn.exec_driver_sql("ALTER TABLE channels DROP COLUMN api_style")
    async with mem_engine.begin() as conn:
        rows = (await conn.exec_driver_sql(sa._ALL_COLUMNS_SQL)).fetchall()
        cols = {}
        for tbl, col in rows:
            cols.setdefault(tbl, set()).add(col)
        pending = sa.plan_alignment(cols)
        targets = [p for p in pending if p[1] == "channels" and p[2] == "api_style"]
        assert len(targets) == 1
        stmt, _t, _c, insert = targets[0]
        await conn.exec_driver_sql(stmt)
        await conn.exec_driver_sql(insert)
        # 登记 + 幂等: INSERT OR IGNORE 重复跑不报错不重复
        await conn.exec_driver_sql(insert)
        got = (await conn.exec_driver_sql(
            "SELECT migration, note FROM schema_migrations "
            "WHERE migration LIKE 'align:channels%'"
        )).fetchall()
    assert got == [("align:channels.api_style", "orm-align")]
    # 列确实回来了且带默认值
    async with mem_engine.connect() as conn:
        val = (await conn.exec_driver_sql(
            "SELECT api_style FROM channels LIMIT 1"
        )).fetchall()
        assert val == []  # 空表无行, 但查询不报 no such column 即可


# ============================================================
# mark_legacy_migrations
# ============================================================

async def test_mark_legacy_migrations_idempotent(mem_engine):
    """INSERT OR IGNORE 等价逻辑: 重复跑不新增, 23 个全登记且顺序稳定。"""
    sa = schema_align
    async with mem_engine.begin() as conn:
        await conn.exec_driver_sql(sa._VERSION_TABLE_DDL)
        for insert in sa._LEGACY_INSERTS:
            await conn.exec_driver_sql(insert)
        n1 = (await conn.exec_driver_sql(
            "SELECT COUNT(*) FROM schema_migrations WHERE note='legacy-manual'"
        )).scalar()
        for insert in sa._LEGACY_INSERTS:
            await conn.exec_driver_sql(insert)
        n2 = (await conn.exec_driver_sql(
            "SELECT COUNT(*) FROM schema_migrations WHERE note='legacy-manual'"
        )).scalar()
    assert n1 == len(sa.LEGACY_MANUAL_MIGRATIONS) == 23
    assert n2 == n1, "INSERT OR IGNORE 重复跑不应新增"
    names = [r[0] for r in (
        await (await mem_engine.connect()).exec_driver_sql(
            "SELECT migration FROM schema_migrations ORDER BY id")
    ).fetchall()]
    assert names == sa.LEGACY_MANUAL_MIGRATIONS


# ============================================================
# init_db 全链路 (真实全局 engine, 隔离 tmp 文件库)
# ============================================================

async def test_init_db_full_chain_on_legacy_db(tmp_path, monkeypatch):
    """老库缺列场景: init_db 一次跑齐 (create_all + align + legacy 标记)。"""
    db_file = tmp_path / "legacy.db"
    # 1) 用 raw sqlite3 造一个"老库": 只有 channels 表且缺 api_style 列
    import sqlite3
    conn = sqlite3.connect(db_file)
    conn.execute(
        "CREATE TABLE channels ("
        " id INTEGER PRIMARY KEY, name TEXT, base_url TEXT)"
    )
    conn.execute(
        "INSERT INTO channels (id, name, base_url) VALUES (1, 'c1', 'http://x')"
    )
    conn.commit()
    conn.close()

    # 2) 全局 engine 指到该库 (conftest 每 test 后 purge 项目模块, 函数内 import)
    from sqlalchemy.ext.asyncio import create_async_engine as _cae
    new_engine = _cae("sqlite+aiosqlite:///%s" % db_file)
    import db.engine as engine_mod
    monkeypatch.setattr(engine_mod, "engine", new_engine)

    from db.init_db import init_db
    await init_db()

    # 3) 断言: api_style 列回来了, 版本表有 orm-align + legacy-manual
    check = __import__("sqlite3").connect(db_file)
    cols = {r[1] for r in check.execute("PRAGMA table_info(channels)").fetchall()}
    assert "api_style" in cols
    assert "status" in cols  # 其他 ORM 列同样补齐
    mig = {r[0]: r[1] for r in check.execute(
        "SELECT migration, note FROM schema_migrations").fetchall()}
    assert mig.get("align:channels.api_style") == "orm-align"
    assert sum(1 for n in mig.values() if n == "legacy-manual") == 23
    check.close()
    await new_engine.dispose()


async def test_init_db_full_chain_idempotent(tmp_path, monkeypatch):
    """init_db 跑两遍: 第二遍零补列零新增版本表行 (幂等)。"""
    db_file = tmp_path / "fresh.db"
    from sqlalchemy.ext.asyncio import create_async_engine as _cae
    new_engine = _cae("sqlite+aiosqlite:///%s" % db_file)
    import db.engine as engine_mod
    monkeypatch.setattr(engine_mod, "engine", new_engine)

    from db.init_db import init_db
    from db.schema_align import get_applied_migrations
    await init_db()
    first = await get_applied_migrations()
    await init_db()
    second = await get_applied_migrations()
    assert first == second, "第二次 init_db 不应新增版本表行"
    # 全新库 create_all 已带全部列 → 无 orm-align 行 (预期);
    # 版本表只登记历史脚本基线
    assert sum(1 for _n, note in first if note == "legacy-manual") == 23
    await new_engine.dispose()
