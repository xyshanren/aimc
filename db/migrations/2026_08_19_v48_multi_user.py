"""V4.8 migration: users + api_keys 表 + usage_logs.caller_id (2026-08-19)

背景:
- 现有设计单人使用 (单一 AIMC_API_KEY), 需扩展到 3~5 人小团队
- 多用户: 每个 user 多个 api_key, usage_logs 记 caller_id 按人聚合
- per-user 日/月 token 限额

本迁移:
1. 新表 users: 用户 (username, role, status, daily/monthly_token_limit)
2. 新表 api_keys: API Key (key_hash, key_prefix, user_id, is_admin, status)
3. usage_logs 加 caller_id INTEGER (NULL=legacy/未鉴权)
4. backfill: 把现有 settings.api_key (单一 key) 导入为 admin user 的 key
   (这样升级后老客户端无需改配置)

用法:
    python db/migrations/2026_08_19_v48_multi_user.py            # dry-run
    python db/migrations/2026_08_19_v48_multi_user.py --apply   # 实际迁移
"""
from __future__ import annotations

import argparse
import hashlib
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
    print(f"users 表存在: {'users' in tables}")
    print(f"api_keys 表存在: {'api_keys' in tables}")

    # usage_logs.caller_id
    ul_cols = conn.execute("PRAGMA table_info('usage_logs')").fetchall()
    has_caller_id = any(c['name'] == 'caller_id' for c in ul_cols)
    print(f"usage_logs.caller_id 存在: {has_caller_id}")

    # 现有 api_key (从 .env / config 读)
    import config
    existing_api_key = config.settings.api_key
    print(f"现有 settings.api_key: {'(已设)' if existing_api_key else '(空)'}")

    # ============ Step 1: users 表 ============
    print("\n=== Step 1: CREATE users 表 ===")
    if 'users' in tables:
        print("  [SKIP] 已存在")
    else:
        print("  [CREATE] (id, username, display_name, role, status, "
              "daily_token_limit, monthly_token_limit, notes, created_at, updated_at)")

    # ============ Step 2: api_keys 表 ============
    print("\n=== Step 2: CREATE api_keys 表 ===")
    if 'api_keys' in tables:
        print("  [SKIP] 已存在")
    else:
        print("  [CREATE] (id, user_id, key_hash, key_prefix, is_admin, "
              "status, label, last_used_at, created_at, updated_at)")

    # ============ Step 3: usage_logs.caller_id ============
    print("\n=== Step 3: ALTER usage_logs ADD caller_id ===")
    if has_caller_id:
        print("  [SKIP] 已存在")
    else:
        print("  [ADD] caller_id INTEGER (NULL=legacy/未鉴权)")

    # ============ Step 4: backfill admin user + key ============
    print("\n=== Step 4: backfill admin user + 迁移现有 api_key ===")
    if existing_api_key and 'users' not in tables:
        print("  [BACKFILL] 创建 admin 用户 + 导入现有 api_key 为 admin key")
        print(f"    username=admin, role=admin")
        print(f"    key_prefix={existing_api_key[:8]}...")
    elif existing_api_key and 'users' in tables:
        # 检查是否已有 admin 用户
        admin = conn.execute(
            "SELECT id FROM users WHERE username='admin'"
        ).fetchone()
        if admin:
            print("  [SKIP] admin 用户已存在")
        else:
            print("  [BACKFILL] 创建 admin 用户 + 导入现有 api_key")
    else:
        print("  [SKIP] 无现有 api_key 或 users 表已存在 (不自动 backfill)")

    if not args.apply:
        print("\n[DRY-RUN] 加 --apply 才执行")
        conn.close()
        return

    # ===== 实际迁移 =====
    print("\n[APPLY] 开始迁移...")
    try:
        conn.execute("BEGIN")

        if 'users' not in tables:
            conn.execute("""
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    display_name TEXT,
                    role TEXT NOT NULL DEFAULT 'user',
                    status TEXT NOT NULL DEFAULT 'active',
                    daily_token_limit INTEGER,
                    monthly_token_limit INTEGER,
                    notes TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            print("  [OK] CREATE users")

        if 'api_keys' not in tables:
            conn.execute("""
                CREATE TABLE api_keys (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    key_hash TEXT NOT NULL UNIQUE,
                    key_prefix TEXT NOT NULL,
                    is_admin INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'active',
                    label TEXT,
                    last_used_at DATETIME,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("CREATE INDEX idx_apikey_hash ON api_keys(key_hash)")
            conn.execute("CREATE INDEX idx_apikey_user_status ON api_keys(user_id, status)")
            print("  [OK] CREATE api_keys")

        if not has_caller_id:
            conn.execute("ALTER TABLE usage_logs ADD COLUMN caller_id INTEGER")
            print("  [OK] ALTER usage_logs ADD caller_id")

        # backfill admin user + 现有 api_key
        if existing_api_key:
            admin = conn.execute(
                "SELECT id FROM users WHERE username='admin'"
            ).fetchone()
            if not admin:
                conn.execute(
                    "INSERT INTO users (username, display_name, role, status, notes) "
                    "VALUES ('admin', '管理员', 'admin', 'active', 'V4.8 迁移自动创建')"
                )
                admin_id = conn.execute(
                    "SELECT id FROM users WHERE username='admin'"
                ).fetchone()['id']
                key_hash = hashlib.sha256(existing_api_key.encode()).hexdigest()
                key_prefix = existing_api_key[:8]
                conn.execute(
                    "INSERT INTO api_keys (user_id, key_hash, key_prefix, is_admin, status, label) "
                    "VALUES (?, ?, ?, 1, 'active', '迁移自 .env AIMC_API_KEY')",
                    (admin_id, key_hash, key_prefix)
                )
                print(f"  [OK] backfill admin user (id={admin_id}) + api_key (prefix={key_prefix}...)")

        conn.execute("COMMIT")

        # 验证
        print("\n=== 验证 ===")
        n_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        n_keys = conn.execute("SELECT COUNT(*) FROM api_keys").fetchone()[0]
        print(f"  users 行数: {n_users}")
        print(f"  api_keys 行数: {n_keys}")
        for u in conn.execute("SELECT id, username, role FROM users").fetchall():
            print(f"    user #{u['id']}: {u['username']} ({u['role']})")

    except Exception as e:
        conn.execute("ROLLBACK")
        print(f"[FAIL] 迁移失败: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
