"""V6.x migration: deepseek_official 开启 sanitize_tool_history (2026-10-27)

背景 (一周观察数据实证, 2026-10-27 盘点):
- tier:vision 组 deepseek-flash@ch1 8 天 657 例 400: 654 例 = DeepSeek 官方
  "An assistant message with 'tool_calls' must be followed by tool messages
  responding to each 'tool_call_id'" (缺失结果配对破坏, 与 MiniMax 2013 同族),
  3 例 = "Invalid assistant message: content or tool_calls must be set"
  (空 assistant)。生产账号直探复现 + 清洗后形态实测 200 (2026-10-27)。
- 2026-10-23 已上产的 _sanitize_tool_history 清洗器直接覆盖这两种形态
  (缺失补合成结果 / 空 assistant 补 content="") — 本迁移只需把
  deepseek_official 的 sanitize_tool_history 置 1。
- 付费渠道白烧止血: 654 例/8 天全是 ch1 付费价行的无效调用。

改动:
1. UPDATE channels SET sanitize_tool_history = 1 WHERE name = 'deepseek_official'
   (幂等; 列本身由 2026_10_23_v6x_minimax_tool_history.py 建)

用法:
    python db/migrations/2026_10_27_v6x_ch1_tool_history.py            # dry-run
    python db/migrations/2026_10_27_v6x_ch1_tool_history.py --apply   # 实际执行
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

TARGET_CHANNEL = "deepseek_official"


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

    cols = [r["name"] for r in conn.execute("PRAGMA table_info(channels)")]
    if "sanitize_tool_history" not in cols:
        print("[FAIL] channels.sanitize_tool_history 列不存在 — 先跑 "
              "2026_10_23_v6x_minimax_tool_history.py")
        conn.close()
        return

    row = conn.execute(
        "SELECT name, sanitize_tool_history FROM channels WHERE name = ?",
        (TARGET_CHANNEL,),
    ).fetchone()
    if not row:
        print(f"[WARN] 渠道 {TARGET_CHANNEL} 不存在, 无需操作")
        conn.close()
        return
    if row["sanitize_tool_history"] == 1:
        print(f"[OK] {TARGET_CHANNEL} 已置 1, 无需操作")
        conn.close()
        return

    print(f"  [TO-UPDATE] {TARGET_CHANNEL} sanitize_tool_history "
          f"{row['sanitize_tool_history']} → 1")
    if not args.apply:
        print("\n[DRY-RUN] 加 --apply 才执行")
        conn.close()
        return

    conn.execute("BEGIN")
    conn.execute(
        "UPDATE channels SET sanitize_tool_history = 1 WHERE name = ?",
        (TARGET_CHANNEL,),
    )
    conn.commit()
    print(f"[OK] {TARGET_CHANNEL} sanitize_tool_history → 1")
    conn.close()


if __name__ == "__main__":
    main()
