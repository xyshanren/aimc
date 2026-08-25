"""V4.9.3 migration: request_transcripts 表 (2026-08-25)

背景:
- 缓存注入 (P1-8 + P3) 上线后, 用户感觉 agent "降智" 但无客观证据
- usage_logs 只记 token 数, 不记请求/响应内容, 无法回溯具体请求
- 新增 request_transcripts 表, 记录发给上游的 request body (注入后) +
  上游 response body, 用于 A/B 对比开/关 cache 的输出质量

控制:
- 由 config.transcript_log_enabled (env: AIMC_TRANSCRIPT_LOG_ENABLED) 开关
- 默认关闭, 仅诊断时开启
- 超期由 scheduler 按 transcript_log_retention_hours 自动清理

用法:
    python db/migrations/2026_08_25_request_transcripts.py            # dry-run
    python db/migrations/2026_08_25_request_transcripts.py --apply   # 实际迁移
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
    print("\n=== Inspect 现有表 ===")
    tables = [r['name'] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )]
    print(f"request_transcripts 表存在: {'request_transcripts' in tables}")

    # ============ Step 1: CREATE request_transcripts ============
    print("\n=== Step 1: CREATE request_transcripts 表 ===")
    if 'request_transcripts' in tables:
        print("  [SKIP] 已存在")
    else:
        print("  [CREATE] (id, channel_id, model, request_body, response_body, "
              "status_code, caller_id, key_id, created_at)")

    if not args.apply:
        print("\n[DRY-RUN] 加 --apply 才执行")
        conn.close()
        return

    # ===== 实际迁移 =====
    print("\n[APPLY] 开始迁移...")
    try:
        conn.execute("BEGIN")

        if 'request_transcripts' not in tables:
            conn.execute("""
                CREATE TABLE request_transcripts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_id INTEGER,
                    model TEXT,
                    request_body TEXT,
                    response_body TEXT,
                    status_code INTEGER,
                    caller_id INTEGER,
                    key_id INTEGER,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute(
                "CREATE INDEX idx_transcripts_created "
                "ON request_transcripts(created_at)"
            )
            conn.execute(
                "CREATE INDEX idx_transcripts_channel_time "
                "ON request_transcripts(channel_id, created_at)"
            )
            print("  [OK] CREATE request_transcripts + 2 indexes")

        conn.execute("COMMIT")

        # 验证
        print("\n=== 验证 ===")
        cols = [c['name'] for c in conn.execute(
            "PRAGMA table_info('request_transcripts')"
        )]
        print(f"  列: {cols}")
        idxs = [r['name'] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='request_transcripts'"
        )]
        print(f"  索引: {idxs}")

    except Exception as e:
        conn.execute("ROLLBACK")
        print(f"[FAIL] 迁移失败: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
