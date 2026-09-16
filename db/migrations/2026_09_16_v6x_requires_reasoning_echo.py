"""V6.5 (2026-09-16): channels 加 requires_reasoning_echo 列

背景 ([[deepseek-reasoning-echo-400]]):
- DeepSeek 官方在带 tools 的 thinking 模式下要求历史**每一轮** assistant 都回传
  reasoning_content, 混合历史 (部分有 rc, 部分没有) 会被上游 400 (retryable=false)
- 跨渠道会话 (tier:flagship 便宜策略在多渠道间跳) 天然产生混合历史 → 整轮 400
- 该渠道级标记置 1 时, proxy 转发前对缺 rc 的 assistant 补占位, 避免 400

安全设计:
- 渠道级开关, 默认 0 (关) — 上产后路由行为不变, 只对显式标记的渠道生效
- 只在"历史混合"时补, 全有或全无 rc 都不改

本改动:
ALTER channels ADD 1 列: requires_reasoning_echo (INTEGER, 默认 0)

用法:
    python db/migrations/2026_09_16_v6x_requires_reasoning_echo.py            # dry-run
    python db/migrations/2026_09_16_v6x_requires_reasoning_echo.py --apply   # 实际迁移
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
    print("\n=== Inspect channels schema ===")
    cols = conn.execute("PRAGMA table_info(channels)").fetchall()
    for c in cols:
        print(f"  {c['name']:35s} {c['type']:15s}")

    print(f"\n当前 channels 行数: {conn.execute('SELECT COUNT(*) FROM channels').fetchone()[0]}")

    # ============ Step 1: ALTER ============
    print("\n=== Step 1: ALTER channels ADD requires_reasoning_echo ===")
    existing = conn.execute(
        "SELECT name FROM pragma_table_info('channels') WHERE name = 'requires_reasoning_echo'"
    ).fetchone()
    if existing:
        print("  [SKIP] requires_reasoning_echo 已存在")
    else:
        print("  [ADD] requires_reasoning_echo INTEGER DEFAULT 0")

    if not args.apply:
        print("\n[DRY-RUN] 加 --apply 才执行")
        conn.close()
        return

    print("\n[APPLY] 开始迁移...")
    try:
        conn.execute("BEGIN")
        if not existing:
            conn.execute(
                "ALTER TABLE channels ADD COLUMN requires_reasoning_echo "
                "INTEGER NOT NULL DEFAULT 0"
            )
            print("  [OK] ALTER channels ADD requires_reasoning_echo INTEGER NOT NULL DEFAULT 0")
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