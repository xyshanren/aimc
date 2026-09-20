"""V6.11 migration: stepfun 渠道 step-5-preview 接入 + step-1o 下线 (2026-09-20)

背景:
- stepfun 官方新增 step-5-preview (1M ctx / 文本+图像+视频输入 / tools / prompt cache)
  https://platform.stepfun.com/docs/zh/guides/models/step-5-preview
- step-1o 过时下线, stepfun_official 渠道价行移除 (usage_logs 历史保留)
- 官方价格 (https://platform.stepfun.com/docs/zh/guides/pricing/details):
    输入 7.0 / 缓存命中 0.35 / 输出 20.0 (元/百万tokens)
- 参考 2026_09_16_v6x_requires_reasoning_echo.py 模式: 检查 + 事务内内联执行

改动:
1. INSERT step-5-preview 到 model_registry (if not exists, 含官方标价 list prices)
2. INSERT step-5-preview price_snapshot 到 stepfun_official 渠道 (if not exists, 含缓存命中价)
3. 移除 step-1o price_snapshot 于 stepfun_official 渠道 (仅该渠道, 其他渠道不动)

用法:
    python db/migrations/2026_09_20_v6x_step5_preview.py            # dry-run
    python db/migrations/2026_09_20_v6x_step5_preview.py --apply   # 实际执行
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

    # ============ 0. 查 stepfun channel id ============
    ch_row = conn.execute(
        "SELECT id, name FROM channels WHERE provider = 'stepfun'"
    ).fetchone()
    if not ch_row:
        print("[FAIL] stepfun 渠道不存在, 无法继续")
        conn.close()
        return
    stepfun_channel_id = ch_row["id"]
    print(f"stepfun 渠道: id={stepfun_channel_id}, name={ch_row['name']}")

    # ============ 1. 检查 model_registry ============
    print("\n=== 1. model_registry 检查 (step-5-preview) ===")
    reg_row = conn.execute(
        "SELECT model_id, family, tier, modality FROM model_registry "
        "WHERE model_id = 'step-5-preview'"
    ).fetchone()
    if reg_row:
        print(f"  [EXISTS] {reg_row['model_id']} family={reg_row['family']} "
              f"tier={reg_row['tier']} modality={reg_row['modality']}")
    else:
        print("  [TO-ADD] step-5-preview (family=step tier=T1 modality=multimodal)")

    # ============ 2. 检查 price_snapshots (新增 step-5-preview) ============
    print("\n=== 2. price_snapshots 检查 (stepfun_official, step-5-preview) ===")
    ps_row = conn.execute(
        "SELECT id, input_price, output_price, cached_input_price FROM price_snapshots "
        "WHERE channel_id = ? AND model = 'step-5-preview' "
        "AND price_type = 'normal' AND time_range = '全天'",
        (stepfun_channel_id,),
    ).fetchone()
    if ps_row:
        print(f"  [EXISTS] input={ps_row['input_price']} output={ps_row['output_price']} "
              f"cache={ps_row['cached_input_price']}")
    else:
        print("  [TO-ADD] 价行: input=7.0 output=20.0 cache=0.35 (元/百万tokens)")

    # ============ 3. 检查 step-1o 下线 (仅 stepfun_official 渠道) ============
    print("\n=== 3. step-1o 下线检查 (stepfun_official) ===")
    del_rows = conn.execute(
        "SELECT id, input_price, output_price FROM price_snapshots "
        "WHERE model = 'step-1o' AND channel_id = ?",
        (stepfun_channel_id,),
    ).fetchall()
    if not del_rows:
        print("  [NONE] step-1o 在 stepfun_official 无价行, 无需处理")
    else:
        for r in del_rows:
            print(f"  [TO-REMOVE] id={r['id']} input={r['input_price']} output={r['output_price']}")

    # 其他渠道的 step-1o 行 — 仅提示, 不动 (超出本迁移范围)
    other_rows = conn.execute(
        "SELECT c.name, ps.input_price, ps.output_price "
        "FROM price_snapshots ps JOIN channels c ON c.id = ps.channel_id "
        "WHERE ps.model = 'step-1o' AND ps.channel_id != ?",
        (stepfun_channel_id,),
    ).fetchall()
    if other_rows:
        print(f"  [INFO] 其他渠道仍有 step-1o 价行 {len(other_rows)} 条, 本迁移不处理:")
        for r in other_rows:
            print(f"    - {r['name']}: input={r['input_price']} output={r['output_price']}")

    if not (reg_row and ps_row and not del_rows):
        if not args.apply:
            print("\n[DRY-RUN] 加 --apply 才执行")
            conn.close()
            return

        print("\n[APPLY] 开始迁移...")
        try:
            conn.execute("BEGIN")
            if not reg_row:
                conn.execute(
                    "INSERT INTO model_registry "
                    "(model_id, family, display_name, tier, capabilities, "
                    "context_length, modality, is_open_source, release_date, "
                    "status, notes, list_input_price, list_output_price) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "step-5-preview",
                        "step",
                        "Step-5 Preview",
                        "T1",
                        '["reasoning", "code", "long_context", "general"]',
                        1000000,
                        "multimodal",
                        0,
                        "2026-09",
                        "preview",
                        "阶跃旗舰 preview: 1M ctx, 文本+图像+视频输入, tools/JSON mode/思考档位; "
                        "标价 7/20 元每百万tokens (缓存命中 0.35)",
                        7.0,
                        20.0,
                    ),
                )
                print("  [OK] 注册 model_registry: step-5-preview")
            if not ps_row:
                conn.execute(
                    "INSERT INTO price_snapshots "
                    "(channel_id, model, price_type, time_range, "
                    "input_price, output_price, cached_input_price, unit, source) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        stepfun_channel_id,
                        "step-5-preview",
                        "normal",
                        "全天",
                        7.0,
                        20.0,
                        0.35,
                        "元/百万tokens",
                        "manual",
                    ),
                )
                print("  [OK] 新增价行: step-5-preview @ stepfun_official")
            if del_rows:
                conn.execute(
                    "DELETE FROM price_snapshots WHERE model = 'step-1o' AND channel_id = ?",
                    (stepfun_channel_id,),
                )
                print(f"  [OK] 移除价行: step-1o @ stepfun_official ({len(del_rows)} 行)")
            conn.execute("COMMIT")
            print("\n[OK] 迁移完成")
        except Exception as e:
            conn.execute("ROLLBACK")
            print(f"[FAIL] 迁移失败: {e}")
            raise
    else:
        print("\n[OK] 所有数据已就绪, 无需操作")

    conn.close()


if __name__ == "__main__":
    main()