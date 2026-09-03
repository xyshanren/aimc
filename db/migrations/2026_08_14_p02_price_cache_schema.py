"""P0-2 migration: price_snapshots + model_registry schema 扩展 (2026-08-14)

背景:
- 硅基流动 /v1/models 返回的 model_id 是上游原始 ID (e.g. "zai-org/GLM-5.2")
- price_snapshots.model 当前存的就是 raw_id, 但 router/proxy 内部用归一化 mid (e.g. "glm-5.2")
- master f090db1 用 channel._actual_model 注入 + proxy 改写 body.model 解决运行时映射
- 但 db 缺 raw_model_id 字段, dashboard 看不到 raw_id 关系

本迁移:
1. price_snapshots 加列:
   - raw_model_id TEXT NULL (上游原始 ID, backfill 来自现有 model 字段)
   - cached_input_price REAL NULL (缓存命中输入价, 元/百万tokens)
   - cache_write_price REAL NULL (缓存写入价, 元/百万tokens)
   - currency TEXT DEFAULT 'CNY' (货币单位, 跨渠道价格比较)
   - is_promotion INTEGER DEFAULT 0 (优惠期价格标记)
2. model_registry 加列:
   - raw_ids TEXT NULL (JSON: {"siliconflow": "zai-org/GLM-5.2", "deepseek": "glm-5.2"})
   - supports_prompt_cache INTEGER DEFAULT 0 (0/1, 是否支持 prompt 缓存)
3. price_snapshots 加 UNIQUE INDEX (channel_id, model) WHERE is_promotion = 0
   - 避免重复 (model 已经在 UNIQUE 索引了, 这里只加 is_promotion=0 的部分)

backfill:
- raw_model_id = model (现状 model 字段就是 raw_id, 等于 backfill)
- currency = 'CNY' (现状默认)
- is_promotion = 0 (现状默认)
- cached_input_price / cache_write_price = NULL (后续 pricing sync job 填充)
- model_registry.raw_ids: 从 model_id 生成 {"_normalized": model_id} (空 dict)
- supports_prompt_cache = 0 (后续 pricing sync job 检测)

用法:
    python db/migrations/2026_08_14_p02_price_cache_schema.py            # dry-run
    python db/migrations/2026_08_14_p02_price_cache_schema.py --apply   # 实际迁移
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

    # ============ Inspect current schema ============
    print("\n=== Inspect current price_snapshots / model_registry schema ===")
    for table in ("price_snapshots", "model_registry"):
        # V6.1 (Mimosa 清理): PRAGMA 不支持参数绑定, 表名是代码内常量, 逐表内联字面量
        if table == "price_snapshots":
            cols = conn.execute("PRAGMA table_info(price_snapshots)").fetchall()
        else:
            cols = conn.execute("PRAGMA table_info(model_registry)").fetchall()
        print(f"\n[{table}] 当前列:")
        for c in cols:
            print(f"  {c['name']:25s} {c['type']:15s} {'NOT NULL' if c['notnull'] else 'NULL':8s} default={c['dflt_value']!r}")

    # ============ Step 1: price_snapshots 加列 ============
    print("\n=== Step 1: price_snapshots 加 raw_model_id / cached_input_price / cache_write_price / currency / is_promotion ===")
    price_alterations = [
        ("raw_model_id", "TEXT", "上游原始 model_id (e.g. zai-org/GLM-5.2)"),
        ("cached_input_price", "REAL", "缓存命中输入价 (元/百万tokens), NULL=不支持缓存或未定价"),
        ("cache_write_price", "REAL", "缓存写入价 (元/百万tokens), NULL=不收费"),
        ("currency", "TEXT", "货币单位 (默认 CNY)"),
        ("is_promotion", "INTEGER", "0=日常价格, 1=优惠期价格 (跟 effective_from/until 联动)"),
    ]
    for col, col_type, comment in price_alterations:
        existing = conn.execute(
            "SELECT name FROM pragma_table_info('price_snapshots') WHERE name = ?", (col,)
        ).fetchone()
        if existing:
            print(f"  [SKIP] {col} 已存在")
            continue
        print(f"  [ADD] {col} {col_type}")

    # ============ Step 2: model_registry 加列 ============
    print("\n=== Step 2: model_registry 加 raw_ids / supports_prompt_cache ===")
    reg_alterations = [
        ("raw_ids", "TEXT", "JSON: 各渠道上游原始 model_id, {\"siliconflow\": \"zai-org/GLM-5.2\", ...}"),
        ("supports_prompt_cache", "INTEGER", "0=不支持 prompt cache, 1=支持"),
    ]
    for col, col_type, comment in reg_alterations:
        existing = conn.execute(
            "SELECT name FROM pragma_table_info('model_registry') WHERE name = ?", (col,)
        ).fetchone()
        if existing:
            print(f"  [SKIP] {col} 已存在")
            continue
        print(f"  [ADD] {col} {col_type}")

    # ============ Step 3: UNIQUE 索引 (避免重复 price_snapshot 行) ============
    print("\n=== Step 3: price_snapshots 加 UNIQUE INDEX (channel_id, model) ===")
    existing_idx = conn.execute("""
        SELECT name FROM sqlite_master
        WHERE type = 'index' AND tbl_name = 'price_snapshots' AND name = 'uq_ps_channel_model'
    """).fetchone()
    if existing_idx:
        print("  [SKIP] uq_ps_channel_model 已存在")
    else:
        print("  [ADD] uq_ps_channel_model ON price_snapshots(channel_id, model)")

    if not args.apply:
        print("\n[DRY-RUN] 加 --apply 才执行")
        conn.close()
        return

    # ===== 实际迁移 =====
    print("\n[APPLY] 开始迁移...")
    try:
        conn.execute("BEGIN")

        # Step 1: 加列
        # V6.1 (Mimosa 清理): 表/列名是代码内常量集合, ALTER 语句逐列内联字面量,
        # 不经 f-string 拼接 (与 2026_09_03_v60_* 迁移同款写法)
        for col, col_type, _ in price_alterations:
            existing = conn.execute(
                "SELECT name FROM pragma_table_info('price_snapshots') WHERE name = ?", (col,)
            ).fetchone()
            if existing:
                continue
            if col == "raw_model_id":
                conn.execute("ALTER TABLE price_snapshots ADD COLUMN raw_model_id TEXT")
            elif col == "cached_input_price":
                conn.execute("ALTER TABLE price_snapshots ADD COLUMN cached_input_price REAL")
            elif col == "cache_write_price":
                conn.execute("ALTER TABLE price_snapshots ADD COLUMN cache_write_price REAL")
            elif col == "currency":
                conn.execute("ALTER TABLE price_snapshots ADD COLUMN currency TEXT")
            elif col == "is_promotion":
                conn.execute("ALTER TABLE price_snapshots ADD COLUMN is_promotion INTEGER")
            print(f"  [OK] ALTER price_snapshots ADD {col} {col_type}")

        for col, col_type, _ in reg_alterations:
            existing = conn.execute(
                "SELECT name FROM pragma_table_info('model_registry') WHERE name = ?", (col,)
            ).fetchone()
            if existing:
                continue
            if col == "raw_ids":
                conn.execute("ALTER TABLE model_registry ADD COLUMN raw_ids TEXT")
            elif col == "supports_prompt_cache":
                conn.execute("ALTER TABLE model_registry ADD COLUMN supports_prompt_cache INTEGER")
            print(f"  [OK] ALTER model_registry ADD {col} {col_type}")

        # Step 1.5: backfill
        print("\n  [BACKFILL] price_snapshots.raw_model_id = model (现状 raw_id 就是 model 字段)")
        conn.execute("""
            UPDATE price_snapshots SET raw_model_id = model
            WHERE raw_model_id IS NULL
        """)
        print(f"  [OK] backfilled price_snapshots.raw_model_id")

        conn.execute("""
            UPDATE price_snapshots SET currency = 'CNY'
            WHERE currency IS NULL
        """)
        print(f"  [OK] backfilled price_snapshots.currency = 'CNY'")

        # Step 2: model_registry backfill (raw_ids 默认空 JSON)
        print("\n  [BACKFILL] model_registry.raw_ids = '{}' (默认空 dict)")
        conn.execute("""
            UPDATE model_registry SET raw_ids = '{}'
            WHERE raw_ids IS NULL
        """)
        print(f"  [OK] backfilled model_registry.raw_ids")

        # Step 3: UNIQUE 索引
        existing_idx = conn.execute("""
            SELECT name FROM sqlite_master
            WHERE type = 'index' AND tbl_name = 'price_snapshots' AND name = 'uq_ps_channel_model'
        """).fetchone()
        if not existing_idx:
            conn.execute("""
                CREATE UNIQUE INDEX uq_ps_channel_model
                ON price_snapshots (channel_id, model)
            """)
            print(f"  [OK] CREATE UNIQUE INDEX uq_ps_channel_model")

        conn.execute("COMMIT")

        # 验证
        print("\n=== 验证 ===")
        for table in ("price_snapshots", "model_registry"):
            # V6.1 (Mimosa 清理): PRAGMA 逐表内联字面量
            if table == "price_snapshots":
                cols = conn.execute("PRAGMA table_info(price_snapshots)").fetchall()
            else:
                cols = conn.execute("PRAGMA table_info(model_registry)").fetchall()
            print(f"\n[{table}] 当前列数: {len(cols)}")
            for c in cols[-5:]:
                print(f"  {c['name']:25s} {c['type']:15s}")

    except Exception as e:
        conn.execute("ROLLBACK")
        print(f"[FAIL] 迁移失败: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
