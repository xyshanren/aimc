"""V6.14 migration: sensenova_official 渠道模型刷新 (2026-09-27)

背景 (用户拍板 2026-09-27, 官方文档站 + 上游 /models 双实证):
- sensenova-6.7-flash-lite 退役: 文档站侧栏已无 (与用户口径一致), /models 也已移除
- 生图模型 sensenova-u1-fast 版本更新 → sensenova-u1.5-fast + sensenova-u1.5-lite
  (文档站侧栏两者都在; /models 暂只有 u1.5-lite, u1.5-fast 未入列属上游滞后,
   行先建好, 未生效前请求 400 → 冷却自愈, 30d 仅 1 次使用无影响面)
- deepseek-v4-pro 退役: 文档站侧栏已无 (/models 暂残留, 以文档站+用户口径为准)
- deepseek-flash 上架: /models 实证 id=规范名 "deepseek-flash" (V4.1-Flash)
- deepseek-v4-flash 旧行保留 (上游 /models 仍承接, 30d 1315 次, 用户未提)

改动 (仅 sensenova_official 渠道, 其他渠道不动):
1. DELETE 价行: deepseek-v4-pro / sensenova-6.7-flash-lite / sensenova-u1-fast
2. INSERT 价行 (if not exists, 0.0/0.0 placeholder 与兄弟行一致):
   - deepseek-flash (规范名行, raw_model_id 留空 — 上游 id 与规范名相同,
     沿用同渠道 glm-5.2/kimi-k3/v4-pro 的行形态)
   - sensenova-u1.5-fast / sensenova-u1.5-lite (生图, 未注册 raw 行=直连专用,
     沿用 u1-fast 行形态; 生图按张计费, 价格行沿用渠道 0 元 placeholder 惯例)

注册表零改动: deepseek-flash 已注册 (T0/multimodal/active);
u1.5 系/6.7-flash-lite 未注册 (直连专用); deepseek-v4-pro 保持注册
(deepseek_official 渠道仍有其规范名价行, 退役只发生在 sensenova)。

影响面提示: deepseek-v4-pro 近 30d 在本渠道 555 次调用 — 删行后其请求
精确路由落 deepseek_official (付费 2/8 元), 属上游退役的自然结果。

情报源 (无需本脚本动 DB): 新增代码源 SenseNovaDocsSource
(web_page, https://platform.sensenova.cn/docs, 1800s) — scheduler 启动时经
upsert_intel_source 按 (provider, url) 自动建行, 部署含重启。

用法:
    python db/migrations/2026_09_27_v6x_sensenova_channel_refresh.py            # dry-run
    python db/migrations/2026_09_27_v6x_sensenova_channel_refresh.py --apply   # 实际执行
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

REMOVED_MODELS = (
    "deepseek-v4-pro",
    "sensenova-6.7-flash-lite",
    "sensenova-u1-fast",
)
# (model, raw_model_id)
ADDED_MODELS = (
    ("deepseek-flash", None),
    ("sensenova-u1.5-fast", "sensenova-u1.5-fast"),
    ("sensenova-u1.5-lite", "sensenova-u1.5-lite"),
)


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

    # ============ 0. 查渠道 ============
    ch_row = conn.execute(
        "SELECT id, name FROM channels WHERE provider = 'sensenova'"
    ).fetchone()
    if not ch_row:
        print("[FAIL] sensenova 渠道不存在, 无法继续")
        conn.close()
        return
    ch_id = ch_row["id"]
    print(f"sensenova 渠道: id={ch_id}, name={ch_row['name']}")

    # ============ 1. 退役检查 ============
    placeholders = ", ".join(["?"] * len(REMOVED_MODELS))
    print("\n=== 1. 退役价行检查 (sensenova_official) ===")
    del_rows = conn.execute(
        f"SELECT id, model FROM price_snapshots "
        f"WHERE model IN ({placeholders}) AND channel_id = ? ORDER BY model",
        (*REMOVED_MODELS, ch_id),
    ).fetchall()
    if not del_rows:
        print("  [NONE] 3 个退役模型在本渠道已无价行")
    else:
        for r in del_rows:
            print(f"  [TO-REMOVE] id={r['id']} {r['model']}")

    other_rows = conn.execute(
        f"SELECT c.name, ps.model FROM price_snapshots ps "
        f"JOIN channels c ON c.id = ps.channel_id "
        f"WHERE ps.model IN ({placeholders}) AND ps.channel_id != ? "
        f"ORDER BY c.name, ps.model",
        (*REMOVED_MODELS, ch_id),
    ).fetchall()
    if other_rows:
        print(f"  [INFO] 其他渠道仍有同名价行 {len(other_rows)} 条, 本迁移不处理:")
        for r in other_rows:
            print(f"    - {r['name']}: {r['model']}")

    # ============ 2. 上架检查 ============
    print("\n=== 2. 上架价行检查 (sensenova_official) ===")
    add_missing = []
    for model, raw in ADDED_MODELS:
        row = conn.execute(
            "SELECT id, input_price, output_price FROM price_snapshots "
            "WHERE channel_id = ? AND model = ? AND price_type = 'normal' AND time_range = '全天'",
            (ch_id, model),
        ).fetchone()
        if row:
            print(f"  [EXISTS] id={row['id']} {model} input={row['input_price']} output={row['output_price']}")
        else:
            add_missing.append((model, raw))
            print(f"  [TO-ADD] {model} (raw_model_id={raw!r}, 0.0/0.0 placeholder)")

    # ============ 3. 组成员核对 (提示级) ============
    print("\n=== 3. 组成员核对 (新旧模型) ===")
    gm_rows = conn.execute(
        f"SELECT group_id, model_id FROM model_group_members "
        f"WHERE model_id IN ({placeholders}, 'deepseek-flash', 'sensenova-u1.5-fast', 'sensenova-u1.5-lite')",
        (*REMOVED_MODELS,),
    ).fetchall()
    seen = set()
    for r in gm_rows:
        if (r["group_id"], r["model_id"]) not in seen:
            seen.add((r["group_id"], r["model_id"]))
            print(f"  [INFO] {r['group_id']} <- {r['model_id']} (本迁移不动组)")
    if not seen:
        print("  [OK] 新旧模型均无组成员行 (deepseek-flash 经其他渠道参与组, 本渠道行不影响)")

    if not del_rows and not add_missing:
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
        if del_rows:
            conn.execute(
                f"DELETE FROM price_snapshots "
                f"WHERE model IN ({placeholders}) AND channel_id = ?",
                (*REMOVED_MODELS, ch_id),
            )
            print(f"  [OK] 移除退役价行 {len(del_rows)} 条 @ sensenova_official")
        for model, raw in add_missing:
            conn.execute(
                "INSERT INTO price_snapshots "
                "(channel_id, model, price_type, time_range, "
                "input_price, output_price, unit, source, raw_model_id, currency) "
                "VALUES (?, ?, 'normal', '全天', 0.0, 0.0, '元/百万tokens', 'manual', ?, 'CNY')",
                (ch_id, model, raw),
            )
            print(f"  [OK] 新增价行: {model} @ sensenova_official")
        conn.execute("COMMIT")
        print("\n[OK] 迁移完成")
    except Exception as e:
        conn.execute("ROLLBACK")
        print(f"[FAIL] 迁移失败: {e}")
        raise

    print("\n=== 迁移后 sensenova_official 价行 ===")
    for r in conn.execute(
        "SELECT model, raw_model_id, input_price, output_price FROM price_snapshots "
        "WHERE channel_id = ? ORDER BY model",
        (ch_id,),
    ):
        print(f"  {r['model']:28s} raw={r['raw_model_id']!r} {r['input_price']}/{r['output_price']}")

    conn.close()


if __name__ == "__main__":
    main()
