"""P0-6 migration: account_quotas + account_quota_usage 表 (2026-08-14)

背景:
- 6 个 LLM 厂商渠道的限额语义都不同, 单一 channel_quota 表不够表达:
  * deepseek_official: RPM + 余额 (余额要定期 sync)
  * glm_bigmodel: 包年包月额度 + 超量按量
  * siliconflow_aggregator: RPM/TPM 按账号 + 余额
  * stepfun_official: RPM + 试用额度
  * sensenova_official: 免费试用 + 并发数限制
  * minimax_compat (TokenPlanMax): 5h rolling + 周 rolling 限额 (2027-06-25 到期)

本迁移:
1. 新表 account_quotas: 限额定义 (一行 = 一个限额维度)
   - account_id FK
   - dimension: 'rpm' | 'tpm' | 'daily_tokens' | 'weekly_tokens' | '5h_tokens' | 'concurrent' | 'balance_cny'
   - limit_value: 限额数值
   - window_seconds: 窗口长度 (NULL = 永久, 如余额)
   - reset_strategy: 'rolling' | 'fixed_daily' | 'fixed_weekly' | 'never'
2. 新表 account_quota_usage: 实时使用量
   - (account_id, dimension) 联合主键
   - window_start: 当前窗口起点
   - used_value: 已用
3. accounts 加 quota_plan TEXT (跟 plan 关联, e.g. 'token_plan_max' / 'pay_as_you_go' / 'free_trial')

注:
- router pre-check: 请求前按 (account, dimension) 检查, 触发限额则跳过该 account
- proxy 累加: 成功后按 response.usage 累加
- rolling window 实现: window_start 之前 N 秒内的请求累计 (需 usage_logs 表辅助)

用法:
    python db/migrations/2026_08_14_p06_account_quotas.py            # dry-run
    python db/migrations/2026_08_14_p06_account_quotas.py --apply   # 实际迁移
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
    print("\n=== Inspect accounts 表 + 新表存在性 ===")
    cols = conn.execute("PRAGMA table_info(accounts)").fetchall()
    has_quota_plan = any(c['name'] == 'quota_plan' for c in cols)
    print(f"accounts.quota_plan 存在: {has_quota_plan}")

    tables = [r['name'] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )]
    print(f"account_quotas 表存在: {'account_quotas' in tables}")
    print(f"account_quota_usage 表存在: {'account_quota_usage' in tables}")

    # ============ Step 1: accounts 加 quota_plan ============
    print("\n=== Step 1: ALTER accounts ADD quota_plan TEXT ===")
    if has_quota_plan:
        print("  [SKIP] 已存在")
    else:
        print("  [ADD] quota_plan TEXT (标识账户所属的 plan 类型)")

    # ============ Step 2: account_quotas 新表 ============
    print("\n=== Step 2: CREATE account_quotas 表 ===")
    if 'account_quotas' in tables:
        print("  [SKIP] 已存在")
    else:
        print("  [CREATE] (id, account_id, dimension, limit_value, window_seconds, reset_strategy, enabled, updated_at, notes)")

    # ============ Step 3: account_quota_usage 新表 ============
    print("\n=== Step 3: CREATE account_quota_usage 表 ===")
    if 'account_quota_usage' in tables:
        print("  [SKIP] 已存在")
    else:
        print("  [CREATE] (account_id, dimension, window_start, used_value, last_updated) PK=(account_id, dimension)")

    # ============ Step 4: backfill accounts.quota_plan ============
    print("\n=== Step 4: backfill quota_plan (基于 channel) ===")
    print("  minimax_compat -> 'token_plan_max'")
    print("  sensenova_official -> 'free_trial'")
    print("  其他 -> 'pay_as_you_go'")

    if not args.apply:
        print("\n[DRY-RUN] 加 --apply 才执行")
        conn.close()
        return

    # ===== 实际迁移 =====
    print("\n[APPLY] 开始迁移...")
    try:
        conn.execute("BEGIN")

        if not has_quota_plan:
            conn.execute("ALTER TABLE accounts ADD COLUMN quota_plan TEXT")
            print("  [OK] ALTER accounts ADD quota_plan TEXT")

        if 'account_quotas' not in tables:
            conn.execute("""
                CREATE TABLE account_quotas (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL REFERENCES accounts(id),
                    dimension TEXT NOT NULL,
                    limit_value REAL NOT NULL,
                    window_seconds INTEGER,
                    reset_strategy TEXT DEFAULT 'rolling',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    notes TEXT
                )
            """)
            conn.execute("""
                CREATE INDEX idx_aq_account ON account_quotas(account_id, enabled)
            """)
            conn.execute("""
                CREATE UNIQUE INDEX uq_aq_account_dimension
                ON account_quotas(account_id, dimension)
            """)
            print("  [OK] CREATE account_quotas")

        if 'account_quota_usage' not in tables:
            conn.execute("""
                CREATE TABLE account_quota_usage (
                    account_id INTEGER NOT NULL,
                    dimension TEXT NOT NULL,
                    window_start DATETIME,
                    used_value REAL NOT NULL DEFAULT 0,
                    last_updated DATETIME DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (account_id, dimension)
                )
            """)
            print("  [OK] CREATE account_quota_usage")

        # backfill quota_plan by channel
        conn.execute("""
            UPDATE accounts
            SET quota_plan = CASE
                WHEN channel_id = (SELECT id FROM channels WHERE name='minimax_compat')
                    THEN 'token_plan_max'
                WHEN channel_id = (SELECT id FROM channels WHERE name='sensenova_official')
                    THEN 'free_trial'
                ELSE 'pay_as_you_go'
            END
            WHERE quota_plan IS NULL
        """)
        print("  [OK] backfilled quota_plan")

        conn.execute("COMMIT")

        # 验证
        print("\n=== 验证 ===")
        n_aq = conn.execute("SELECT COUNT(*) FROM account_quotas").fetchone()[0]
        n_aqu = conn.execute("SELECT COUNT(*) FROM account_quota_usage").fetchone()[0]
        n_plans = conn.execute(
            "SELECT quota_plan, COUNT(*) FROM accounts GROUP BY quota_plan"
        ).fetchall()
        print(f"  account_quotas 行数: {n_aq} (期望 0 — quota_config 后续 seed)")
        print(f"  account_quota_usage 行数: {n_aqu} (期望 0 — 启动时 lazy init)")
        print(f"  accounts 按 quota_plan:")
        for row in n_plans:
            print(f"    {row[0]}: {row[1]}")

    except Exception as e:
        conn.execute("ROLLBACK")
        print(f"[FAIL] 迁移失败: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
