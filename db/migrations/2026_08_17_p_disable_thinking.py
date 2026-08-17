"""V4.6.4 (2026-08-17): channels 表加 disable_thinking_pass_through 列

背景:
- minimax-m2.7 / minimax-m2.5 等上游默认 thinking 输出 (即使 request 不带 enable_thinking)
- thinking 块吃 token + 加 latency, 对 agent loop 不友好
- proxy 层剥离请求里的 thinking/enable_thinking/reasoning_effort 字段

本改动:
1. ALTER channels 加 disable_thinking_pass_through INTEGER DEFAULT 1
   - 0=不剥离 (上游默认 thinking 输出)
   - 1=剥离 (proxy 层主动移除 thinking 相关字段, 默认)
2. 所有现有 channel 默认 1 (个人项目偏好)

用法:
    python db/migrations/2026_08_17_p_disable_thinking.py            # dry-run
    python db/migrations/2026_08_17_p_disable_thinking.py --apply   # 实际迁移
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
    print("\n=== Inspect channels schema ===")
    cols = conn.execute("PRAGMA table_info(channels)").fetchall()
    has_disable_thinking = any(c['name'] == 'disable_thinking_pass_through' for c in cols)
    print(f"  disable_thinking_pass_through: {has_disable_thinking}")

    if has_disable_thinking:
        print("  [INFO] 列已存在, 跳过 ALTER")

    print("\n当前 channels:")
    # 兼容 ALTER 之前 (列还不存在)
    if has_disable_thinking:
        select_sql = "SELECT id, name, supports_prompt_cache, disable_thinking_pass_through FROM channels ORDER BY id"
        for row in conn.execute(select_sql):
            print(f"  [{row['id']}] {row['name']:30s} cache={row['supports_prompt_cache']} "
                  f"disable_thinking={row['disable_thinking_pass_through']}")
    else:
        for row in conn.execute("SELECT id, name, supports_prompt_cache FROM channels ORDER BY id"):
            print(f"  [{row['id']}] {row['name']:30s} cache={row['supports_prompt_cache']} "
                  f"disable_thinking=N/A (列待添加)")

    if not args.apply:
        print("\n[DRY-RUN] 加 --apply 才执行")
        conn.close()
        return

    # Apply
    print("\n[APPLY] 开始迁移...")
    try:
        conn.execute("BEGIN")

        if not has_disable_thinking:
            conn.execute(
                "ALTER TABLE channels ADD COLUMN disable_thinking_pass_through INTEGER DEFAULT 1"
            )
            print("  [OK] ALTER channels ADD disable_thinking_pass_through INTEGER DEFAULT 1")

        # 所有现有 channel 默认 1 (个人偏好: thinking 剥离)
        # (新列 default=1 已经在 ALTER 时自动 backfill)
        # 显式 UPDATE 一次确保
        conn.execute(
            "UPDATE channels SET disable_thinking_pass_through = 1 "
            "WHERE disable_thinking_pass_through IS NULL"
        )

        conn.execute("COMMIT")

        # 验证
        print("\n=== 验证 ===")
        for row in conn.execute(
            "SELECT name, supports_prompt_cache, disable_thinking_pass_through FROM channels ORDER BY id"
        ):
            print(f"  [{row['name']}] cache={row['supports_prompt_cache']} "
                  f"disable_thinking={row['disable_thinking_pass_through']}")

    except Exception as e:
        conn.execute("ROLLBACK")
        print(f"[FAIL] 迁移失败: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()