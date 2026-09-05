"""V6.x (2026-09-05): usage_logs 加 reasoning_tokens 列

背景:
- 思考型模型 (kimi-k3 / glm-5.3 / deepseek-v4-pro / qwen3-vl-*-thinking / minimax-m3)
  的思考 token 之前是账本盲区: usage 里只记 output_tokens 总数, 看板无法区分
  思考消耗与正文输出
- 数据来源: OpenAI 风格 usage.completion_tokens_details.reasoning_tokens
  (kimi/glm/deepseek 等均按此风格返回); 上游不返回时记 NULL

观察期决策 (V6.x 关思考评估, 详见 docs/thinking-control-plan.md):
- 先上计量, 观察生产 reasoning_tokens 占 output_tokens 比例
- 占比持续 >30% 或单请求思考 token 显著拖慢 agent loop 时, 再考虑注入模式
  (主动发 enable_thinking:false 等显式关闭参数)

本改动:
ALTER usage_logs ADD 1 列: reasoning_tokens (INTEGER, NULL=上游未返回)

用法:
    python db/migrations/2026_09_05_v6x_reasoning_tokens.py            # dry-run
    python db/migrations/2026_09_05_v6x_reasoning_tokens.py --apply   # 实际迁移
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
    print("\n=== Inspect usage_logs schema ===")
    cols = conn.execute("PRAGMA table_info(usage_logs)").fetchall()
    for c in cols:
        print(f"  {c['name']:30s} {c['type']:15s}")

    print(f"\n当前 usage_logs 行数: {conn.execute('SELECT COUNT(*) FROM usage_logs').fetchone()[0]}")

    # ============ Step 1: ALTER ============
    print("\n=== Step 1: ALTER usage_logs ADD reasoning_tokens ===")
    existing = conn.execute(
        "SELECT name FROM pragma_table_info('usage_logs') WHERE name = 'reasoning_tokens'"
    ).fetchone()
    if existing:
        print("  [SKIP] reasoning_tokens 已存在")
    else:
        print("  [ADD] reasoning_tokens INTEGER")

    if not args.apply:
        print("\n[DRY-RUN] 加 --apply 才执行")
        conn.close()
        return

    print("\n[APPLY] 开始迁移...")
    try:
        conn.execute("BEGIN")
        if not existing:
            conn.execute("ALTER TABLE usage_logs ADD COLUMN reasoning_tokens INTEGER")
            print("  [OK] ALTER usage_logs ADD reasoning_tokens INTEGER")
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
