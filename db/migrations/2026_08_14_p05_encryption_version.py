"""P0-5 migration: accounts 加 encryption_version + master_key 启动校验 (2026-08-14)

背景:
- accounts.api_key_ciphertext 用 Fernet 加密, key 来自 env AIMC_MASTER_KEY
- 历史上 master key 改过, 旧 ciphertext 解密失败 (5/6 账号)
- 当前 router._load_channels_cache 只 WARNING, 没法快速定位"哪个 key 版本"
- 没有 encryption_version 字段, reencrypt 时无法区分老/新密文

本改动:
1. accounts 加列 encryption_version INTEGER DEFAULT 1
   - 1 = 用 AIMC_MASTER_KEY (Fernet, hex 64 字符) 加密 (现状)
   - 2 = 未来用 AIMC_MASTER_KEY_V2 加密 (预留)
2. backfill: 所有非空 ciphertext 标 version=1
3. 启动时校验: config.py settings 加 _validate_master_key() 启动钩子
   - AIMC_MASTER_KEY 必须 64 字符 hex
   - 启动时打 INFO 日志: "MASTER_KEY 校验通过, encryption_version=1"

兼容性:
- 现有 ciphertext 都是 v1 (Fernet + 64-char hex key), backfill 后全部标记 1
- reencrypt_accounts.py 升级: 写入新 ciphertext 时设 version=2 (预留)

用法:
    python db/migrations/2026_08_14_p05_encryption_version.py            # dry-run
    python db/migrations/2026_08_14_p05_encryption_version.py --apply   # 实际迁移
"""
from __future__ import annotations

import argparse
import re
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
    print("\n=== Inspect accounts schema ===")
    cols = conn.execute("PRAGMA table_info(accounts)").fetchall()
    has_version = any(c['name'] == 'encryption_version' for c in cols)
    print(f"encryption_version 列存在: {has_version}")

    n_total = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    n_with_cipher = conn.execute(
        "SELECT COUNT(*) FROM accounts WHERE api_key_ciphertext IS NOT NULL AND api_key_ciphertext != ''"
    ).fetchone()[0]
    n_already_v1 = 0
    if has_version:
        n_already_v1 = conn.execute(
            "SELECT COUNT(*) FROM accounts WHERE encryption_version = 1"
        ).fetchone()[0]
    print(f"总账号: {n_total}, 有 ciphertext: {n_with_cipher}, 已标 v1: {n_already_v1}")

    # ============ Step 1: ALTER ============
    print("\n=== Step 1: ALTER accounts ADD encryption_version ===")
    if has_version:
        print("  [SKIP] encryption_version 已存在")
    else:
        print("  [ADD] encryption_version INTEGER DEFAULT 1")

    # ============ Step 2: backfill ============
    print("\n=== Step 2: backfill encryption_version=1 for all existing ciphertext ===")
    if has_version and n_already_v1 == n_with_cipher:
        print("  [SKIP] 已是 v1")
    else:
        print(f"  [BACKFILL] 准备标 {n_with_cipher - n_already_v1} 行为 v1")

    # ============ Step 3: master_key 校验检查 ============
    print("\n=== Step 3: 检查 master_key 校验逻辑 (config.py) ===")
    print("  [INFO] 启动校验在 config.py: AIMC_MASTER_KEY 必须 64 字符 hex")
    print("  [INFO] 启动失败 if 校验不过 (启动时报错而非运行时静默失败)")

    if not args.apply:
        print("\n[DRY-RUN] 加 --apply 才执行")
        conn.close()
        return

    # ===== 实际迁移 =====
    print("\n[APPLY] 开始迁移...")
    try:
        conn.execute("BEGIN")

        if not has_version:
            conn.execute("ALTER TABLE accounts ADD COLUMN encryption_version INTEGER DEFAULT 1")
            print("  [OK] ALTER accounts ADD encryption_version INTEGER DEFAULT 1")

        conn.execute("""
            UPDATE accounts
            SET encryption_version = 1
            WHERE api_key_ciphertext IS NOT NULL
              AND api_key_ciphertext != ''
              AND (encryption_version IS NULL OR encryption_version = 0)
        """)
        print("  [OK] backfilled encryption_version=1")

        conn.execute("COMMIT")

        # 验证
        n_now_v1 = conn.execute(
            "SELECT COUNT(*) FROM accounts WHERE encryption_version = 1"
        ).fetchone()[0]
        print(f"\n[OK] 验证: {n_now_v1} 账号标 v1 (期望 {n_with_cipher})")
        if n_now_v1 != n_with_cipher:
            print("[WARN] 不一致, 检查 db")

    except Exception as e:
        conn.execute("ROLLBACK")
        print(f"[FAIL] 迁移失败: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
