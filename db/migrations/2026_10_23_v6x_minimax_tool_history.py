"""V6.x migration: channels 加 sanitize_tool_history 列 (MiniMax 2013 tool 配对清洗)
              + request_transcripts 加 message_skeleton 列 (非 200 取证骨架)

背景 (2026-10-23 生产实证):
- MiniMax 对 tool_call/tool_result 配对做严格校验, 三种破坏形态直探全部 400 (2013):
  孤儿 (id 匹配不到前置 tool_calls) / 错位 (结果被其他消息隔开) / 缺失 (并行调用
  只回部分结果)。跨模型混历史 (组内 failover) + harness 裁剪历史都会产生。
  生产 4 天 67 例 (strong 组 m3.1-flash 为主力后出现), V6.18 400 failover 已能
  换路保成功, 但每次多一跳 + 客户端多等一轮 — 本迁移从源头修。
- 修复模式同 V6.5 requires_reasoning_echo (DeepSeek reasoning 回传 400):
  渠道级标记 sanitize_tool_history=1, proxy 转发前归一化:
  孤儿转 user 文本 / 错位结果归位 / 缺失补 "[tool result unavailable]"。
  清洗后输出形态 (含连续 user 消息) 已直探 MiniMax 上游实测 200。
- message_skeleton: request_body 50000 字符截断对图片重的请求会在 base64 处截断,
  消息结构不可见 (取证 2013 时实证) — 非 200 时附百字节级骨架, 保取证能力。

改动:
1. ALTER channels ADD 1 列: sanitize_tool_history (INTEGER, 默认 0)
2. UPDATE minimax_compat SET sanitize_tool_history = 1
3. ALTER request_transcripts ADD 1 列: message_skeleton (TEXT)

用法:
    python db/migrations/2026_10_23_v6x_minimax_tool_history.py            # dry-run
    python db/migrations/2026_10_23_v6x_minimax_tool_history.py --apply   # 实际迁移
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

TARGET_CHANNEL = "minimax_compat"


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

    # ============ 1. channels.sanitize_tool_history ============
    print("\n=== 1. channels.sanitize_tool_history ===")
    ch_cols = [r["name"] for r in conn.execute("PRAGMA table_info(channels)")]
    need_alter_channels = "sanitize_tool_history" not in ch_cols
    if need_alter_channels:
        print("  [TO-ALTER] channels 加列 sanitize_tool_history (INTEGER 默认 0)")
    else:
        print("  [OK] 列已存在")

    flag_row = None
    if not need_alter_channels:
        flag_row = conn.execute(
            "SELECT name, sanitize_tool_history FROM channels WHERE name = ?",
            (TARGET_CHANNEL,),
        ).fetchone()
        if flag_row and flag_row["sanitize_tool_history"] == 1:
            print(f"  [OK] {TARGET_CHANNEL} 已置 1")
        elif flag_row:
            print(f"  [TO-UPDATE] {TARGET_CHANNEL} 当前 {flag_row['sanitize_tool_history']} → 1")
        else:
            print(f"  [WARN] 渠道 {TARGET_CHANNEL} 不存在 (跳过置位)")

    # ============ 2. request_transcripts.message_skeleton ============
    print("\n=== 2. request_transcripts.message_skeleton ===")
    tr_cols = [r["name"] for r in conn.execute("PRAGMA table_info(request_transcripts)")]
    need_alter_transcripts = "message_skeleton" not in tr_cols
    if need_alter_transcripts:
        print("  [TO-ALTER] request_transcripts 加列 message_skeleton (TEXT)")
    else:
        print("  [OK] 列已存在")

    pending = (
        need_alter_channels
        or need_alter_transcripts
        or (flag_row is not None and flag_row["sanitize_tool_history"] != 1)
    )
    if not pending:
        print("\n[OK] 所有数据已就绪, 无需操作")
        conn.close()
        return

    if not args.apply:
        print("\n[DRY-RUN] 加 --apply 才执行")
        conn.close()
        return

    print("\n[APPLY] 开始迁移...")
    try:
        conn.execute("BEGIN")
        if need_alter_channels:
            conn.execute("ALTER TABLE channels ADD COLUMN sanitize_tool_history INTEGER DEFAULT '0' NOT NULL")
            print("  [OK] channels.sanitize_tool_history 加列")
        if need_alter_transcripts:
            conn.execute("ALTER TABLE request_transcripts ADD COLUMN message_skeleton TEXT")
            print("  [OK] request_transcripts.message_skeleton 加列")
        flag_now = conn.execute(
            "SELECT sanitize_tool_history FROM channels WHERE name = ?",
            (TARGET_CHANNEL,),
        ).fetchone()
        if flag_now is not None and flag_now[0] != 1:
            conn.execute(
                "UPDATE channels SET sanitize_tool_history = 1 WHERE name = ?",
                (TARGET_CHANNEL,),
            )
            print(f"  [OK] {TARGET_CHANNEL} sanitize_tool_history → 1")
        conn.execute("COMMIT")
        print("\n[OK] 迁移完成")
    except Exception as e:
        conn.execute("ROLLBACK")
        print(f"[FAIL] 迁移失败: {e}")
        raise

    conn.close()


if __name__ == "__main__":
    main()
