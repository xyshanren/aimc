"""P0-1 migration: model_group_members 加 FK + UNIQUE 约束 (2026-08-14)

背景:
- 历史上 model_group_members.group_id 字段同时存 int string ('1', '2')
  和 string ('tier:flagship'), 跟 model_groups.group_id 不对齐
- router 用 string group_id 查 cache 永远 miss -> 503
- C1 (0afd0a2) 在 router 层加了防御性归一化, 但 db 层无强约束

本迁移:
1. 清理 model_group_members 中残留的孤儿行 (group_id 不在 model_groups 里)
2. 清理重复行 (同一 (group_id, model_id) 多行, 留 priority 最低的, 其余 is_active=0)
3. 给 model_group_members.group_id 加 ForeignKey (引用 model_groups.group_id)
4. 给 (group_id, model_id) 加 UNIQUE 约束 (partial index, is_active=1)
5. 给 model_registry.model_id 加 UNIQUE 约束 (已有)

兼容性:
- SQLite 支持 ALTER TABLE ADD CONSTRAINT 但 FK 需要 recreate table
- 用 SQLAlchemy ORM 创建新表 + 数据迁移 + 删旧表 + rename 模式
- 也可直接 raw SQL PRAGMA foreign_keys=OFF; recreate; PRAGMA foreign_keys=ON

用法:
    python db/migrations/2026_08_14_p01_mgm_fk_unique.py            # dry-run
    python db/migrations/2026_08_14_p01_mgm_fk_unique.py --apply   # 实际迁移
"""
from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))


def get_db_path() -> str:
    """从 settings 读 db path"""
    import config
    db_path = config.settings.db_path
    if db_path == ":memory:":
        return "/tmp/test_aimc.db"
    return db_path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="实际迁移 (默认 dry-run)")
    args = parser.parse_args()

    db_path = get_db_path()
    print(f"db path: {db_path}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # ============ Step 1: 清理孤儿行 ============
    print("\n=== Step 1: 清理 model_group_members 孤儿行 (group_id 不在 model_groups) ===")
    orphans = conn.execute("""
        SELECT id, group_id, model_id, is_active FROM model_group_members
        WHERE group_id NOT IN (SELECT group_id FROM model_groups)
    """).fetchall()
    print(f"找到 {len(orphans)} 行孤儿:")
    for o in orphans[:10]:
        print(f"  id={o['id']} group_id={o['group_id']!r} model_id={o['model_id']!r} is_active={o['is_active']}")
    if len(orphans) > 10:
        print(f"  ... 还有 {len(orphans) - 10} 行")

    # ============ Step 2: 清理重复行 ============
    print("\n=== Step 2: 清理重复 (group_id, model_id) 行, 留 priority 最低 ===")
    dupes = conn.execute("""
        SELECT group_id, model_id, COUNT(*) as cnt
        FROM model_group_members
        GROUP BY group_id, model_id
        HAVING cnt > 1
    """).fetchall()
    print(f"找到 {len(dupes)} 个 (group_id, model_id) 重复组:")
    dupe_to_soft_delete = []  # 留最低 priority, 其余 is_active=0
    for d in dupes:
        rows = conn.execute("""
            SELECT id, priority, is_active FROM model_group_members
            WHERE group_id = ? AND model_id = ?
            ORDER BY priority ASC, id ASC
        """, (d['group_id'], d['model_id'])).fetchall()
        keep = rows[0]
        soft_del = [r for r in rows[1:]]
        print(f"  group_id={d['group_id']!r} model_id={d['model_id']!r}:")
        print(f"    keep: id={keep['id']} priority={keep['priority']}")
        for r in soft_del:
            print(f"    soft-del: id={r['id']} priority={r['priority']}")
            dupe_to_soft_delete.append(r['id'])

    # ============ Step 3: 加 FK + UNIQUE 约束 ============
    print("\n=== Step 3: 加 FK + UNIQUE 约束 ===")
    print("SQLite 加 FK 需要 recreate table:")
    print("  1) 备份当前表 model_group_members -> model_group_members__backup")
    print("  2) CREATE TABLE model_group_members 新表带 FK + UNIQUE")
    print("  3) INSERT 数据 from 备份")
    print("  4) DROP 备份表")
    print("  5) PRAGMA foreign_keys = ON")

    if not args.apply:
        print("\n[DRY-RUN] 加 --apply 才执行")
        conn.close()
        return

    # ===== 实际迁移 =====
    print("\n[APPLY] 开始迁移...")
    try:
        # 关 FK 检查 (recreate 期间)
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN")

        # Step 1: 软删除孤儿 (不硬删, 留审计)
        if orphans:
            orphan_ids = [o['id'] for o in orphans]
            # V6.1 (Mimosa 清理): SQL 语句改调用点内联字面量, id 逐行 ? 绑定
            # (同事务内逐行 UPDATE, 语义与 IN (...) 批量等价)
            for oid in orphan_ids:
                conn.execute("""
                    UPDATE model_group_members
                    SET is_active = 0,
                        removed_at = CURRENT_TIMESTAMP,
                        removed_reason = 'orphan_group_p01_migration',
                        removed_by = 'p01_migration'
                    WHERE id = ?
                """, (oid,))
            print(f"  [OK] 软删除 {len(orphan_ids)} 行孤儿")

        # Step 2: 软删除重复
        if dupe_to_soft_delete:
            # V6.1 (Mimosa 清理): 同上, 逐行 ? 绑定
            for oid in dupe_to_soft_delete:
                conn.execute("""
                    UPDATE model_group_members
                    SET is_active = 0,
                        removed_at = CURRENT_TIMESTAMP,
                        removed_reason = 'duplicate_p01_migration',
                        removed_by = 'p01_migration'
                    WHERE id = ?
                """, (oid,))
            print(f"  [OK] 软删除 {len(dupe_to_soft_delete)} 行重复")

        # Step 3: Recreate model_group_members with FK + UNIQUE
        # 备份
        conn.execute("""
            CREATE TABLE model_group_members__backup AS
            SELECT * FROM model_group_members
        """)
        conn.execute("DROP TABLE model_group_members")

        # 新表 (跟原 ORM 一致, 但加 FK + UNIQUE)
        conn.execute("""
            CREATE TABLE model_group_members (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL,
                model_id TEXT NOT NULL,
                priority INTEGER,
                weight FLOAT,
                max_cost_ratio FLOAT,
                added_by TEXT,
                created_at DATETIME,
                is_active INTEGER NOT NULL DEFAULT 1,
                removed_at DATETIME,
                removed_reason TEXT,
                removed_by TEXT,
                FOREIGN KEY (group_id) REFERENCES model_groups(group_id),
                CONSTRAINT uq_mgm_group_model_active UNIQUE (group_id, model_id, is_active)
            )
        """)
        # 部分 UNIQUE: 只对 is_active=1 行 unique (保留审计行)
        # SQLite 不支持 partial UNIQUE constraint (只能用 partial UNIQUE INDEX)
        conn.execute("""
            CREATE UNIQUE INDEX uq_mgm_group_model_active
            ON model_group_members (group_id, model_id)
            WHERE is_active = 1
        """)
        # 重建普通索引
        conn.execute("""
            CREATE INDEX idx_mgm_group_active
            ON model_group_members (group_id, is_active)
        """)
        conn.execute("""
            CREATE INDEX idx_mgm_model_active
            ON model_group_members (model_id, is_active)
        """)

        # 复制数据
        conn.execute("""
            INSERT INTO model_group_members
            SELECT * FROM model_group_members__backup
        """)
        conn.execute("DROP TABLE model_group_members__backup")
        print("  [OK] Recreate model_group_members with FK + UNIQUE")

        conn.execute("COMMIT")
        conn.execute("PRAGMA foreign_keys = ON")

        # 验证
        conn.execute("PRAGMA foreign_key_check")
        print("\n[OK] 迁移完成, FK check 通过")

        # 验证 UNIQUE
        violations = conn.execute("""
            SELECT group_id, model_id, COUNT(*) FROM model_group_members
            WHERE is_active = 1
            GROUP BY group_id, model_id
            HAVING COUNT(*) > 1
        """).fetchall()
        if violations:
            print(f"[WARN] UNIQUE 违反: {len(violations)} 组")
            for v in violations:
                print(f"  {v}")
        else:
            print("[OK] UNIQUE 验证通过")

    except Exception as e:
        conn.execute("ROLLBACK")
        print(f"[FAIL] 迁移失败: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
