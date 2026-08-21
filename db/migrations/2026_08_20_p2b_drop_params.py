"""P2-B (2026-08-20): channels 表加 drop_params 列

背景:
- 跨模型转发时, 某些上游 channel 对特定参数返回 400 (e.g. reasoning_effort,
  max_tokens 超限, 不支持的工具格式)
- P2-A 修复了 vision 路由层的 400, 但参数不兼容导致的 400 需要通用机制
- LiteLLM 的 drop_params 模式: 转发前剥离目标 channel 不支持的参数

设计:
- deny-list (而非 allow-list): 默认空=全透传 (当前行为), 发现 400 时加参数名
- JSON-as-text 存 list[str], e.g. '["reasoning_effort","max_tokens"]'
- proxy 层在 thinking strip 之后过滤 body 顶层字段
- messages/model/stream 永远保留 (ALWAYS_KEEP_PARAMS)

用法:
    python db/migrations/2026_08_20_p2b_drop_params.py            # dry-run
    python db/migrations/2026_08_20_p2b_drop_params.py --apply     # 实际迁移
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

    print("\n=== Inspect channels schema ===")
    cols = conn.execute("PRAGMA table_info(channels)").fetchall()
    has_drop_params = any(c['name'] == 'drop_params' for c in cols)
    print(f"  drop_params: {has_drop_params}")

    if has_drop_params:
        print("  [INFO] 列已存在, 跳过 ALTER")

    print("\n当前 channels:")
    if has_drop_params:
        select_sql = (
            "SELECT id, name, supports_prompt_cache, disable_thinking_pass_through, "
            "drop_params FROM channels ORDER BY id"
        )
        for row in conn.execute(select_sql):
            print(f"  [{row['id']}] {row['name']:30s} cache={row['supports_prompt_cache']} "
                  f"disable_thinking={row['disable_thinking_pass_through']} "
                  f"drop_params={row['drop_params'] or '(空)'}")
    else:
        for row in conn.execute(
            "SELECT id, name, supports_prompt_cache, disable_thinking_pass_through "
            "FROM channels ORDER BY id"
        ):
            print(f"  [{row['id']}] {row['name']:30s} cache={row['supports_prompt_cache']} "
                  f"disable_thinking={row['disable_thinking_pass_through']} "
                  f"drop_params=N/A (列待添加)")

    if not args.apply:
        print("\n[DRY-RUN] 加 --apply 才执行")
        conn.close()
        return

    print("\n[APPLY] 开始迁移...")
    try:
        conn.execute("BEGIN")

        if not has_drop_params:
            conn.execute(
                "ALTER TABLE channels ADD COLUMN drop_params TEXT"
            )
            print("  [OK] ALTER channels ADD drop_params TEXT")

        conn.execute("COMMIT")

        print("\n=== 验证 ===")
        for row in conn.execute(
            "SELECT name, supports_prompt_cache, disable_thinking_pass_through, "
            "drop_params FROM channels ORDER BY id"
        ):
            print(f"  [{row['name']}] cache={row['supports_prompt_cache']} "
                  f"disable_thinking={row['disable_thinking_pass_through']} "
                  f"drop_params={row['drop_params'] or '(空)'}")

    except Exception as e:
        conn.execute("ROLLBACK")
        print(f"[FAIL] 迁移失败: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
