"""V6.4 (2026-09-11): channels 加 disable_thinking_inject 列 (思考控制 Phase B)

背景 (docs/thinking-control-plan.md §3):
- Phase A 计量观察结论: 思考/输出比 W35 47.5% + W36 48.8% 连续两个自然周 >30%
  (触发条件 1 成立), dsv4-pro/glm-5.3 等延迟显著 (条件 2 方向一致) → 用户拍板
  立项 Phase B (注入模式)
- 注入 = proxy 在请求 body 顶层主动注入 enable_thinking=false (OpenAI 风格),
  压制模型原生思考; 与既有请求侧剥离 (disable_thinking_pass_through) 互补

安全设计:
- 渠道级开关, 默认 0 (关) — 上产后路由行为不变, 各渠道开谁由用户看数据决定
- 客户端显式传了 enable_thinking 时不覆盖 (尊重客户端)
- 上游不认该参数时依赖既有 drop_params/auto_drop_params 防 400

本改动:
ALTER channels ADD 1 列: disable_thinking_inject (INTEGER, 默认 0)

用法:
    python db/migrations/2026_09_11_v6x_thinking_inject.py            # dry-run
    python db/migrations/2026_09_11_v6x_thinking_inject.py --apply   # 实际迁移
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
    print("\n=== Step 1: ALTER channels ADD disable_thinking_inject ===")
    existing = conn.execute(
        "SELECT name FROM pragma_table_info('channels') WHERE name = 'disable_thinking_inject'"
    ).fetchone()
    if existing:
        print("  [SKIP] disable_thinking_inject 已存在")
    else:
        print("  [ADD] disable_thinking_inject INTEGER DEFAULT 0")

    if not args.apply:
        print("\n[DRY-RUN] 加 --apply 才执行")
        conn.close()
        return

    print("\n[APPLY] 开始迁移...")
    try:
        conn.execute("BEGIN")
        if not existing:
            conn.execute(
                "ALTER TABLE channels ADD COLUMN disable_thinking_inject "
                "INTEGER NOT NULL DEFAULT 0"
            )
            print("  [OK] ALTER channels ADD disable_thinking_inject INTEGER NOT NULL DEFAULT 0")
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
