"""V6.x migration: modelscope_inference 开启 sanitize_tool_history (2026-10-29)

背景 (生产实录, 2026-10-29):
- tier:vision 组 ModelScope 当日 33 例 400, 绝大多数 = ModelScope 上游
  Pydantic 严格校验拒收历史消息里 content=null 的 assistant 消息:
  "20 validation errors: ... ChatCompletionDeveloperMessageParam,
  'content', 'str' ... 'input': None" — 两种形态 (transcripts.message_skeleton
  取证): 带 tool_calls 且 content=None (`a~tc[...]~none`) 与空 assistant
  (`assi~none`)。其余少数为单请求 >4 张图的渠道硬上限 (failover 换路, 不归
  清洗器管)。
- 同日已上产的 _sanitize_tool_history 扩展覆盖 content=None 两种形态
  (assistant 补空串, 不再限于双缺空 assistant) — 本迁移只需把
  modelscope_inference 的 sanitize_tool_history 置 1。
- 清洗后形态已直探 ModelScope 上游实测 (content="" + tool_calls, 2026-10-29)。

改动:
1. UPDATE channels SET sanitize_tool_history = 1 WHERE name = 'modelscope_inference'
   (幂等; 列本身由 2026_10_23_v6x_minimax_tool_history.py 建)

用法:
    python db/migrations/2026_10_29_v6x_modelscope_tool_history.py            # dry-run
    python db/migrations/2026_10_29_v6x_modelscope_tool_history.py --apply   # 实际执行
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

TARGET_CHANNEL = "modelscope_inference"


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
