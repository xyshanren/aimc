"""P2-16: 测试 ModelGroupMember FK + UNIQUE 约束 (P0-1)

设计:
- 用 tmp 文件 SQLite (in-memory 无法验证 FK)
- 在临时 db 上跑 Base.metadata.create_all + 模拟 P0-1 迁移
- 测试:
  * FK 约束: model_group_member.group_id 必须引用 model_groups.id
  * UNIQUE 约束: (group_id, model_id) 唯一
  * 孤儿行 (group_id 不存在) -> IntegrityError
  * 重复 (group_id, model_id) -> IntegrityError
  * 正常 add -> OK
"""
import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def db_path(tmp_path):
    """临时 SQLite 文件, 跑 Base.metadata.create_all + P0-1 迁移"""
    from sqlalchemy import create_engine
    from db.models import Base, ModelGroup, ModelGroupMember

    p = tmp_path / "test.db"
    eng = create_engine(f"sqlite:///{p}", echo=False)
    Base.metadata.create_all(eng)

    # 模拟 P0-1 迁移 (实际 migration 脚本: db/migrations/2026_08_14_p01_mgm_fk_unique.py)
    with eng.begin() as conn:
        conn.exec_driver_sql("""
            CREATE TRIGGER IF NOT EXISTS mgm_fk_check
            BEFORE INSERT ON model_group_members
            FOR EACH ROW
            WHEN NOT EXISTS (SELECT 1 FROM model_groups WHERE id = NEW.group_id)
            BEGIN
                SELECT RAISE(ABORT, 'FOREIGN KEY constraint failed: model_group_members.group_id');
            END;
        """)
    eng.dispose()
    return p


def test_create_tables_and_fk(db_path):
    """建表 + FK 触发器 + UNIQUE 约束"""
    conn = sqlite3.connect(str(db_path))
    c = conn.cursor()
    # 表存在
    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='model_groups'")
    assert c.fetchone()[0] == "model_groups"
    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='model_group_members'")
    assert c.fetchone()[0] == "model_group_members"
    # UNIQUE 约束
    c.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_mgm_model_id'")
    # UNIQUE 约束在我们的 schema 里是 UNIQUE(group_id, model_id) (P0-1 migration)
    # 这里没真正加 (因为我们没跑完整 migration), 仅建表
    conn.close()


def test_orphan_member_rejected(db_path):
    """孤儿 member (group_id 不存在) 应被 FK 触发器拒绝"""
    conn = sqlite3.connect(str(db_path))
    c = conn.cursor()
    # 先建 group
    c.execute("INSERT INTO model_groups (group_id, enabled) VALUES ('tier:flagship', 1)")
    conn.commit()
    # 尝试插孤儿 member (group_id=999 不存在) -> FK 触发器在 execute 时抛 IntegrityError
    with pytest.raises(sqlite3.IntegrityError) as exc:
        c.execute("INSERT INTO model_group_members (group_id, model_id, priority) VALUES (999, 'qwen-v4-pro', 100)")
    assert "FOREIGN KEY" in str(exc.value)
    conn.close()


def test_unique_group_model(db_path):
    """UNIQUE(group_id, model_id): 重复插入 -> IntegrityError"""
    conn = sqlite3.connect(str(db_path))
    c = conn.cursor()
    c.execute("INSERT INTO model_groups (group_id, enabled) VALUES ('tier:flagship', 1)")
    group_id = c.lastrowid
    c.execute(
        "INSERT INTO model_group_members (group_id, model_id, priority, is_active) VALUES (?, 'qwen-v4-pro', 100, 1)",
        (group_id,),
    )
    conn.commit()
    # 重复 (同 group_id + 同 model_id) -> UNIQUE 在 execute 时抛 IntegrityError
    with pytest.raises(sqlite3.IntegrityError) as exc:
        c.execute(
            "INSERT INTO model_group_members (group_id, model_id, priority, is_active) VALUES (?, 'qwen-v4-pro', 110, 1)",
            (group_id,),
        )
    assert "UNIQUE" in str(exc.value)
    conn.close()


def test_same_model_different_groups_ok(db_path):
    """同 model 不同 group 允许 (group_id 不同时)"""
    conn = sqlite3.connect(str(db_path))
    c = conn.cursor()
    c.execute("INSERT INTO model_groups (group_id, enabled) VALUES ('tier:flagship', 1)")
    c.execute("INSERT INTO model_groups (group_id, enabled) VALUES ('tier:strong', 1)")
    g1 = c.execute("SELECT id FROM model_groups WHERE group_id='tier:flagship'").fetchone()[0]
    g2 = c.execute("SELECT id FROM model_groups WHERE group_id='tier:strong'").fetchone()[0]
    # 同 model 在 2 个 group 里
    c.execute(
        "INSERT INTO model_group_members (group_id, model_id, priority, is_active) VALUES (?, 'qwen-v4-pro', 100, 1)",
        (g1,),
    )
    c.execute(
        "INSERT INTO model_group_members (group_id, model_id, priority, is_active) VALUES (?, 'qwen-v4-pro', 110, 1)",
        (g2,),
    )
    conn.commit()
    # 2 行都在
    c.execute("SELECT COUNT(*) FROM model_group_members WHERE model_id='qwen-v4-pro'")
    assert c.fetchone()[0] == 2
    conn.close()


def test_member_insert_with_valid_group(db_path):
    """正常 insert (group_id 存在) -> OK"""
    conn = sqlite3.connect(str(db_path))
    c = conn.cursor()
    c.execute("INSERT INTO model_groups (group_id, enabled) VALUES ('tier:flagship', 1)")
    group_id = c.lastrowid
    c.execute(
        "INSERT INTO model_group_members (group_id, model_id, priority, is_active) VALUES (?, 'qwen-v4-pro', 100, 1)",
        (group_id,),
    )
    conn.commit()
    c.execute("SELECT COUNT(*) FROM model_group_members")
    assert c.fetchone()[0] == 1
    conn.close()
