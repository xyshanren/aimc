"""方案 A (2026-08-14): 建 model_change_suggestions + group_change_suggestions 表

背景:
- 当前 model_group_members 静态, 模型升级 (e.g. GLM-5.3) 需要手工改
- 需要 \"建议系统\" 主动分析 model_cards / usage_logs / price_snapshots
  生成可执行建议, 人工 review + dashboard apply

本改动:
CREATE 2 张表:
1. model_change_suggestions - 单 model 调整 (tier 升降 / 加进 group / 移除)
2. group_change_suggestions - 组级别调整 (新增 / 删除 / 合并)

用法:
    python db/migrations/2026_08_14_p_suggestions_tables.py            # dry-run
    python db/migrations/2026_08_14_p_suggestions_tables.py --apply   # 实际迁移
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))


def get_db_path() -> str:
    import config
    db_path = config.settings.db_path
    if db_path == ":memory:":
        return "/tmp/test_aimc.db"
    return db_path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    db_path = get_db_path()
    print(f"db path: {db_path}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # ============ Inspect ============
    print("\n=== Inspect tables ===")
    tables = [r['name'] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )]
    has_mcs = "model_change_suggestions" in tables
    has_gcs = "group_change_suggestions" in tables
    print(f"  model_change_suggestions 存在: {has_mcs}")
    print(f"  group_change_suggestions 存在: {has_gcs}")

    # ============ Step 1: model_change_suggestions ============
    print("\n=== Step 1: CREATE model_change_suggestions ===")
    if not has_mcs:
        print("  [CREATE] (id, model_id, change_type, suggested_value, current_value, reason, confidence, based_on, status, created_at, reviewed_at, reviewed_by, applied_at)")
    else:
        print("  [SKIP] 已存在")

    # ============ Step 2: group_change_suggestions ============
    print("\n=== Step 2: CREATE group_change_suggestions ===")
    if not has_gcs:
        print("  [CREATE] (id, group_id, change_type, suggested_config, target_groups, reason, confidence, based_on, status, created_at, reviewed_at, reviewed_by, applied_at)")
    else:
        print("  [SKIP] 已存在")

    if not args.apply:
        print("\n[DRY-RUN] 加 --apply 才执行")
        conn.close()
        return

    # ===== 实际迁移 =====
    print("\n[APPLY] 开始迁移...")
    try:
        conn.execute("BEGIN")

        if not has_mcs:
            conn.execute("""
                CREATE TABLE model_change_suggestions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    model_id TEXT NOT NULL,
                    change_type TEXT NOT NULL,
                    suggested_value TEXT,
                    current_value TEXT,
                    reason TEXT,
                    confidence REAL,
                    based_on TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    reviewed_at DATETIME,
                    reviewed_by TEXT,
                    applied_at DATETIME
                )
            """)
            conn.execute("""
                CREATE INDEX idx_mcs_status ON model_change_suggestions(status, created_at)
            """)
            conn.execute("""
                CREATE INDEX idx_mcs_model ON model_change_suggestions(model_id, change_type)
            """)
            print("  [OK] CREATE model_change_suggestions + 2 索引")

        if not has_gcs:
            conn.execute("""
                CREATE TABLE group_change_suggestions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL,
                    change_type TEXT NOT NULL,
                    suggested_config TEXT,
                    target_groups TEXT,
                    reason TEXT,
                    confidence REAL,
                    based_on TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    reviewed_at DATETIME,
                    reviewed_by TEXT,
                    applied_at DATETIME
                )
            """)
            conn.execute("""
                CREATE INDEX idx_gcs_status ON group_change_suggestions(status, created_at)
            """)
            conn.execute("""
                CREATE INDEX idx_gcs_group ON group_change_suggestions(group_id, change_type)
            """)
            print("  [OK] CREATE group_change_suggestions + 2 索引")

        conn.execute("COMMIT")
        print("\n[OK] 迁移完成")
    except Exception as e:
        conn.execute("ROLLBACK")
        print(f"[FAIL] 迁移失败: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
