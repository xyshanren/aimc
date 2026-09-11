"""V6.x (2026-09-09): model_feedback_summary 加 feedback_score_no_retry 列

背景:
- 周报反馈分被两类缺陷扭曲: (a) 5 分钟同账号相邻启发式把正常顺序流量
  误判为重试 (生产实测重试率 85.8%); (b) 线性延迟扣分 0.01*p95 使
  P95>10s 的模型分数直接地板为 0
- 修复后重试项改用真实信号 (同账号同模型 + 前一次失败 + 秒级间隔),
  延迟改对数分档扣分并对思考类模型豁免
- 用户决策 (2026-09-09): 重试项保留与否先用双分数并行跑一段对比再定 —
  feedback_score 含真实重试信号, feedback_score_no_retry 完全去掉重试项

本改动:
ALTER model_feedback_summary ADD 1 列: feedback_score_no_retry (REAL, NULL=未计算)

用法:
    python db/migrations/2026_09_09_v6x_feedback_score_no_retry.py            # dry-run
    python db/migrations/2026_09_09_v6x_feedback_score_no_retry.py --apply   # 实际迁移
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
    print("\n=== Inspect model_feedback_summary schema ===")
    cols = conn.execute("PRAGMA table_info(model_feedback_summary)").fetchall()
    for c in cols:
        print(f"  {c['name']:30s} {c['type']:15s}")

    print(f"\n当前 model_feedback_summary 行数: {conn.execute('SELECT COUNT(*) FROM model_feedback_summary').fetchone()[0]}")

    # ============ Step 1: ALTER ============
    print("\n=== Step 1: ALTER model_feedback_summary ADD feedback_score_no_retry ===")
    existing = conn.execute(
        "SELECT name FROM pragma_table_info('model_feedback_summary') WHERE name = 'feedback_score_no_retry'"
    ).fetchone()
    if existing:
        print("  [SKIP] feedback_score_no_retry 已存在")
    else:
        print("  [ADD] feedback_score_no_retry REAL")

    if not args.apply:
        print("\n[DRY-RUN] 加 --apply 才执行")
        conn.close()
        return

    print("\n[APPLY] 开始迁移...")
    try:
        conn.execute("BEGIN")
        if not existing:
            conn.execute("ALTER TABLE model_feedback_summary ADD COLUMN feedback_score_no_retry REAL")
            print("  [OK] ALTER model_feedback_summary ADD feedback_score_no_retry REAL")
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
