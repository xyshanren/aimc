"""阶段2 (2026-08-14): job_health 表 + scheduler hook 写入

背景:
- 当前 apscheduler_jobs 只有 next_run_time, 没有 last_run / last_success / last_error
- dashboard 想显示 \"数据新鲜度\" 需要知道每个 job 何时成功跑过

本改动:
1. CREATE job_health 表
   - job_id (PK) 对应 apscheduler_jobs.id
   - last_run_at: 上次执行时间
   - last_success_at: 上次成功时间
   - last_error_at: 上次失败时间
   - last_error_msg: TEXT (错误信息)
   - last_duration_ms: 上次执行耗时
   - total_runs / total_successes / total_failures: 累计计数
2. 不动 apscheduler_jobs (它由 apscheduler 自己管)

用法:
    python db/migrations/2026_08_14_p2_job_health_table.py            # dry-run
    python db/migrations/2026_08_14_p2_job_health_table.py --apply   # 实际迁移
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

    # Inspect
    tables = [r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )]
    has_jh = "job_health" in tables
    print(f"\njob_health 表存在: {has_jh}")

    if not has_jh:
        print("\n[CREATE] job_health (job_id PK, last_run_at, last_success_at, last_error_at, last_error_msg, last_duration_ms, total_runs, total_successes, total_failures)")

    if not args.apply:
        print("\n[DRY-RUN] 加 --apply 才执行")
        conn.close()
        return

    print("\n[APPLY] 开始迁移...")
    try:
        conn.execute("BEGIN")
        if not has_jh:
            conn.execute("""
                CREATE TABLE job_health (
                    job_id TEXT PRIMARY KEY,
                    last_run_at DATETIME,
                    last_success_at DATETIME,
                    last_error_at DATETIME,
                    last_error_msg TEXT,
                    last_duration_ms INTEGER,
                    total_runs INTEGER NOT NULL DEFAULT 0,
                    total_successes INTEGER NOT NULL DEFAULT 0,
                    total_failures INTEGER NOT NULL DEFAULT 0
                )
            """)
            conn.execute("""
                CREATE INDEX idx_job_health_last_success
                ON job_health(last_success_at)
            """)
            print("  [OK] CREATE job_health + 索引")
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
