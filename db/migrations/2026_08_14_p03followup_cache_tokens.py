"""P0-3 follow-up (2026-08-14): usage_logs 加 cached_tokens 列

背景:
- P0-2 schema 加了 price_snapshots.cached_input_price / cache_write_price
- 但 usage_logs 没记录每次请求的 cache token 数 → dashboard 看不到 cache 命中率
- P1-8 真正实现 cache_control headers 注入前, 先把数据通路准备好

本改动:
ALTER usage_logs ADD 3 列:
- cached_input_tokens: OpenAI-style (usage.prompt_tokens_details.cached_tokens)
- cache_read_input_tokens: Anthropic-style (usage.cache_read_input_tokens)
- cache_creation_input_tokens: Anthropic-style (usage.cache_creation_input_tokens)

用法:
    python db/migrations/2026_08_14_p03followup_cache_tokens.py            # dry-run
    python db/migrations/2026_08_14_p03followup_cache_tokens.py --apply   # 实际迁移
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
    print("\n=== Step 1: ALTER usage_logs ADD 3 列 ===")
    new_cols = [
        ("cached_input_tokens", "INTEGER",
         "OpenAI-style: 缓存命中 token 数 (usage.prompt_tokens_details.cached_tokens)"),
        ("cache_read_input_tokens", "INTEGER",
         "Anthropic-style: 缓存读取 token 数 (usage.cache_read_input_tokens)"),
        ("cache_creation_input_tokens", "INTEGER",
         "Anthropic-style: 缓存写入 token 数 (usage.cache_creation_input_tokens)"),
    ]
    for col, ctype, comment in new_cols:
        existing = conn.execute(
            "SELECT name FROM pragma_table_info('usage_logs') WHERE name = ?", (col,)
        ).fetchone()
        if existing:
            print(f"  [SKIP] {col} 已存在")
        else:
            print(f"  [ADD] {col} {ctype}")

    if not args.apply:
        print("\n[DRY-RUN] 加 --apply 才执行")
        conn.close()
        return

    print("\n[APPLY] 开始迁移...")
    try:
        conn.execute("BEGIN")
        for col, ctype, _ in new_cols:
            existing = conn.execute(
                "SELECT name FROM pragma_table_info('usage_logs') WHERE name = ?", (col,)
            ).fetchone()
            if existing:
                continue
            # V6.1 (Mimosa 清理): 列名是代码内常量集合, ALTER 语句逐列内联字面量,
            # 不经 f-string 拼接 (与 2026_09_03_v60_* 迁移同款写法)
            if col == "cached_input_tokens":
                conn.execute("ALTER TABLE usage_logs ADD COLUMN cached_input_tokens INTEGER")
            elif col == "cache_read_input_tokens":
                conn.execute("ALTER TABLE usage_logs ADD COLUMN cache_read_input_tokens INTEGER")
            elif col == "cache_creation_input_tokens":
                conn.execute("ALTER TABLE usage_logs ADD COLUMN cache_creation_input_tokens INTEGER")
            print(f"  [OK] ALTER usage_logs ADD {col} {ctype}")
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
