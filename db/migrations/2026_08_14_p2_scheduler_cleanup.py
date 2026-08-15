"""阶段2 (2026-08-14): 清理 apscheduler_jobs 旧 crawl_* jobs

背景:
- 升级 scheduler 用 batch_crawl_<provider> 替代每 source 1 个 job (31 → ~15)
- apscheduler 不自动删旧 job, 需手动清理
- 保留: batch_*, refresh_route_cache, check_key_rotation, cross_source_detection,
        monthly_official_audit, weekly_suggest_changes

用法:
    python db/migrations/2026_08_14_p2_scheduler_cleanup.py            # dry-run
    python db/migrations/2026_08_14_p2_scheduler_cleanup.py --apply   # 实际删
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


KEEP_PATTERNS = (
    "batch_crawl_",
    "refresh_route_cache",
    "check_key_rotation",
    "cross_source_detection",
    "monthly_official_audit",
    "weekly_suggest_changes",
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    db_path = get_db_path()
    print(f"db path: {db_path}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # 查全部 jobs
    rows = conn.execute("SELECT id FROM apscheduler_jobs").fetchall()
    print(f"\n=== apscheduler_jobs 总 {len(rows)} 行 ===")

    keep = []
    remove = []
    for r in rows:
        if any(r["id"].startswith(p) or r["id"] == p for p in KEEP_PATTERNS):
            keep.append(r["id"])
        else:
            remove.append(r["id"])

    print(f"\n保留 ({len(keep)}):")
    for k in keep:
        print(f"  ✓ {k}")
    print(f"\n删除 ({len(remove)}):")
    for r_id in remove[:30]:
        print(f"  ✗ {r_id}")
    if len(remove) > 30:
        print(f"  ... 还有 {len(remove) - 30}")

    if not args.apply:
        print(f"\n[DRY-RUN] 加 --apply 才删 (上面 {len(remove)} 行)")
        conn.close()
        return

    print("\n[APPLY] 开始删除...")
    try:
        conn.execute("BEGIN")
        for r_id in remove:
            conn.execute("DELETE FROM apscheduler_jobs WHERE id = ?", (r_id,))
        conn.execute("COMMIT")
        print(f"[OK] 删除 {len(remove)} 行")
    except Exception as e:
        conn.execute("ROLLBACK")
        print(f"[FAIL] 删除失败: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
