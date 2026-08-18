"""P1-12 (2026-08-14): channels 表加 api_base + api_style + supports_prompt_cache 列

背景:
- 当前 channels 表只有 base_url (1 个), 改 base_url 要 restart service
- SENSENOVA_BASE_URL 走 .env 覆盖 db 是 hack (V4.6.4 master f9ce404 实现)
- 多协议支持 (Anthropic vs OpenAI) 没显式区分 — 全部按 OpenAI 处理
- 缓存价格 schema 已就位 (P0-2), 但 channel 是否支持 cache 没标记

本改动:
1. ALTER channels 加 3 列:
   - api_base TEXT NULL: 实际 base_url (覆盖 db.base_url, 不需要 restart)
   - api_style TEXT DEFAULT 'openai': 'openai' | 'anthropic' (路由协议风格)
   - supports_prompt_cache INTEGER DEFAULT 0: 0=不支持, 1=支持 (硅基流动/DeepSeek 支持, qwen/智谱部分支持)
2. backfill:
   - api_base = NULL (现状用 base_url, dashboard 改后再填)
   - api_style = 'openai' (现状所有渠道都按 OpenAI 处理)
   - supports_prompt_cache: 按 channel.name 推断
     * siliconflow_aggregator -> 1 (SF 支持 prompt cache)
     * deepseek_official -> 1 (deepseek 支持)
     * stepfun_official -> 0
     * glm_bigmodel -> 0 (智谱暂时不支持)
     * sensenova_official -> 0
     * minimax_compat -> 0

注:
- 跟 SENSENOVA_BASE_URL env 的兼容: router 用 api_base ?? base_url ?? SENSENOVA_BASE_URL ?? base_url
  即: db.api_base 优先, 然后 db.base_url, 然后 env override, 然后 fallback db.base_url
- 这是 P1-8 prompt cache 真正实现的前置 (channel.api_style 决定 cache_control headers 格式)

用法:
    python db/migrations/2026_08_14_p112_channel_api_base.py            # dry-run
    python db/migrations/2026_08_14_p112_channel_api_base.py --apply   # 实际迁移
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
    has_api_base = any(c['name'] == 'api_base' for c in cols)
    has_api_style = any(c['name'] == 'api_style' for c in cols)
    has_supports_cache = any(c['name'] == 'supports_prompt_cache' for c in cols)
    print(f"  api_base: {has_api_base}, api_style: {has_api_style}, supports_prompt_cache: {has_supports_cache}")

    print("\n当前 channels:")
    for row in conn.execute("SELECT id, name, base_url FROM channels"):
        print(f"  [{row['id']}] {row['name']:30s} {row['base_url']}")

    # ============ Step 1: ALTER ============
    print("\n=== Step 1: ALTER channels ADD 3 列 ===")
    for col, ctype, default, comment in [
        ("api_base", "TEXT", None, "实际 base_url (覆盖 db.base_url, 不需要 restart service)"),
        ("api_style", "TEXT", "'openai'", "协议风格: openai / anthropic"),
        ("supports_prompt_cache", "INTEGER", "0", "0=不支持 prompt cache, 1=支持 (硅基流动/DeepSeek 支持)"),
    ]:
        existing = conn.execute(
            "SELECT name FROM pragma_table_info('channels') WHERE name = ?", (col,)
        ).fetchone()
        if existing:
            print(f"  [SKIP] {col} 已存在")
        else:
            if default:
                print(f"  [ADD] {col} {ctype} DEFAULT {default}")
            else:
                print(f"  [ADD] {col} {ctype}")

    # ============ Step 2: backfill supports_prompt_cache ============
    print("\n=== Step 2: backfill supports_prompt_cache 按 channel.name ===")
    SUPPORT_CACHE_CHANNELS = {"siliconflow_aggregator", "deepseek_official"}
    print(f"  推断支持 cache 的渠道: {SUPPORT_CACHE_CHANNELS}")

    if not args.apply:
        print("\n[DRY-RUN] 加 --apply 才执行")
        conn.close()
        return

    # ===== 实际迁移 =====
    print("\n[APPLY] 开始迁移...")
    try:
        conn.execute("BEGIN")

        if not has_api_base:
            conn.execute("ALTER TABLE channels ADD COLUMN api_base TEXT")
            print("  [OK] ALTER channels ADD api_base TEXT")
        if not has_api_style:
            conn.execute("ALTER TABLE channels ADD COLUMN api_style TEXT DEFAULT 'openai'")
            print("  [OK] ALTER channels ADD COLUMN api_style TEXT DEFAULT 'openai'")
        if not has_supports_cache:
            conn.execute("ALTER TABLE channels ADD COLUMN supports_prompt_cache INTEGER DEFAULT 0")
            print("  [OK] ALTER channels ADD supports_prompt_cache INTEGER DEFAULT 0")

        # backfill supports_prompt_cache
        for ch_name in SUPPORT_CACHE_CHANNELS:
            result = conn.execute(
                "UPDATE channels SET supports_prompt_cache = 1 WHERE name = ?",
                (ch_name,)
            )
            if result.rowcount:
                print(f"  [OK] {ch_name}: supports_prompt_cache=1 ({result.rowcount} row)")

        conn.execute("COMMIT")

        # 验证
        print("\n=== 验证 ===")
        for row in conn.execute(
            "SELECT name, base_url, api_base, api_style, supports_prompt_cache FROM channels ORDER BY id"
        ):
            print(f"  [{row['name']}] base_url={row['base_url']} api_base={row['api_base']} "
                  f"style={row['api_style']} cache={row['supports_prompt_cache']}")

    except Exception as e:
        conn.execute("ROLLBACK")
        print(f"[FAIL] 迁移失败: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
