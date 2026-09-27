"""V6.14 migration: minimax_compat 渠道 minimax-m3.1-flash-preview 接入 + 5 个老模型下线 (2026-09-27)

背景:
- MiniMax 新发布 minimax-m3.1-flash-preview (多模态, 用户口径)。
  生产直连实证 (2026-09-27, api.minimaxi.com/v1):
    /models 尚未收录该模型, 但 chat completions 用规范名直测 200,
    响应 model=MiniMax-M3.1-Flash-Preview (上游大小写不敏感)。
- 5 个老模型过时下线 (用户拍板): minimax-m2.5 / minimax-m2.5-highspeed /
  minimax-m2.1 / minimax-m2.1-highspeed / minimax-m2 — 仅删 minimax_compat 渠道价行,
  usage_logs 历史保留。
- siliconflow_aggregator 的 minimax-m2.5 规范名兜底行 (2.1/8.4 cache 0.21) 不受影响,
  model_registry 的 minimax-m2.5 条目 (raw_ids/list_price) 因此保留。
- 组成员零涉及: 5 个老模型不在任何 model_group_members 行内 (生产核实 2026-09-27),
  minimax-m3 (tier:balanced + tier:vision) / minimax-m2.7 (tier:code) 不动。
- 新模型未进组等口令; ctx 暂按 M3 线 1M 填, 待官方文档复核。
- 参考 2026_09_20_v6x_step5_preview.py 模式: 检查 + 事务内内联执行

改动:
1. INSERT minimax-m3.1-flash-preview 到 model_registry (if not exists, status='preview')
2. INSERT minimax-m3.1-flash-preview price_snapshot 到 minimax_compat 渠道
   (if not exists, 0.0/0.0 placeholder 与兄弟行一致, raw_model_id 填同名与兄弟行一致)
3. 移除 5 个老模型 price_snapshot 于 minimax_compat 渠道 (仅该渠道)

用法:
    python db/migrations/2026_09_27_v6x_minimax_m31_flash_preview.py            # dry-run
    python db/migrations/2026_09_27_v6x_minimax_m31_flash_preview.py --apply   # 实际执行
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

NEW_MODEL = "minimax-m3.1-flash-preview"
REMOVED_MODELS = (
    "minimax-m2.5",
    "minimax-m2.5-highspeed",
    "minimax-m2.1",
    "minimax-m2.1-highspeed",
    "minimax-m2",
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

    # ============ 0. 查 minimax channel id ============
    ch_row = conn.execute(
        "SELECT id, name FROM channels WHERE provider = 'minimax'"
    ).fetchone()
    if not ch_row:
        print("[FAIL] minimax 渠道不存在, 无法继续")
        conn.close()
        return
    minimax_channel_id = ch_row["id"]
    print(f"minimax 渠道: id={minimax_channel_id}, name={ch_row['name']}")

    # ============ 1. 检查 model_registry ============
    print(f"\n=== 1. model_registry 检查 ({NEW_MODEL}) ===")
    reg_row = conn.execute(
        "SELECT model_id, family, tier, modality, status FROM model_registry "
        "WHERE model_id = ?",
        (NEW_MODEL,),
    ).fetchone()
    if reg_row:
        print(f"  [EXISTS] {reg_row['model_id']} family={reg_row['family']} "
              f"tier={reg_row['tier']} modality={reg_row['modality']} status={reg_row['status']}")
    else:
        print("  [TO-ADD] minimax-m3.1-flash-preview (family=minimax tier=T2 "
              "modality=multimodal status=preview ctx=1M)")

    # ============ 2. 检查 price_snapshots (新增) ============
    print(f"\n=== 2. price_snapshots 检查 (minimax_compat, {NEW_MODEL}) ===")
    ps_row = conn.execute(
        "SELECT id, input_price, output_price, raw_model_id FROM price_snapshots "
        "WHERE channel_id = ? AND model = ? "
        "AND price_type = 'normal' AND time_range = '全天'",
        (minimax_channel_id, NEW_MODEL),
    ).fetchone()
    if ps_row:
        print(f"  [EXISTS] id={ps_row['id']} input={ps_row['input_price']} "
              f"output={ps_row['output_price']} raw={ps_row['raw_model_id']}")
    else:
        print("  [TO-ADD] 价行: 0.0/0.0 placeholder (与兄弟行一致), raw_model_id=同名")

    # ============ 3. 检查 5 个老模型下线 (仅 minimax_compat 渠道) ============
    print("\n=== 3. 老模型下线检查 (minimax_compat) ===")
    placeholders = ", ".join(["?"] * len(REMOVED_MODELS))
    del_rows = conn.execute(
        f"SELECT id, model, input_price, output_price FROM price_snapshots "
        f"WHERE model IN ({placeholders}) AND channel_id = ? "
        f"ORDER BY model",
        (*REMOVED_MODELS, minimax_channel_id),
    ).fetchall()
    if not del_rows:
        print("  [NONE] 5 个老模型在 minimax_compat 无价行, 无需处理")
    else:
        for r in del_rows:
            print(f"  [TO-REMOVE] id={r['id']} {r['model']} "
                  f"input={r['input_price']} output={r['output_price']}")

    # 其他渠道的同名行 — 仅提示, 不动 (siliconflow 的 minimax-m2.5 是有意兜底)
    other_rows = conn.execute(
        f"SELECT c.name, ps.model, ps.input_price, ps.output_price "
        f"FROM price_snapshots ps JOIN channels c ON c.id = ps.channel_id "
        f"WHERE ps.model IN ({placeholders}) AND ps.channel_id != ? "
        f"ORDER BY c.name, ps.model",
        (*REMOVED_MODELS, minimax_channel_id),
    ).fetchall()
    if other_rows:
        print(f"  [INFO] 其他渠道仍有老模型价行 {len(other_rows)} 条, 本迁移不处理:")
        for r in other_rows:
            print(f"    - {r['name']}: {r['model']} input={r['input_price']} output={r['output_price']}")

    # ============ 4. 组成员核对 (提示级, 本迁移不改组) ============
    print("\n=== 4. 组成员核对 (model_group_members, minimax) ===")
    gm_rows = conn.execute(
        f"SELECT group_id, model_id FROM model_group_members "
        f"WHERE model_id IN ({placeholders}) OR model_id = ?",
        (*REMOVED_MODELS, NEW_MODEL),
    ).fetchall()
    if gm_rows:
        for r in gm_rows:
            print(f"  [INFO] {r['group_id']} <- {r['model_id']} (本迁移不动组)")
    else:
        print("  [OK] 新旧模型均无组成员行, 组路由零涉及")

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
                    "status, notes) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "minimax-m3.1-flash-preview",
                        "minimax",
                        "MiniMax M3.1 Flash Preview",
                        "T2",
                        '["reasoning", "code", "general"]',
                        1000000,
                        "multimodal",
                        0,
                        "2026-09",
                        "preview",
                        "M3 线 flash preview, 多模态 (用户口径); 上游 chat 直测 200 "
                        "(2026-09-27, /models 未收录); ctx 暂对齐 M3 线 1M 待复核; "
                        "未进组等口令",
                    ),
                )
                print(f"  [OK] 注册 model_registry: {NEW_MODEL} (status=preview)")
            if not ps_row:
                conn.execute(
                    "INSERT INTO price_snapshots "
                    "(channel_id, model, price_type, time_range, "
                    "input_price, output_price, unit, source, raw_model_id, currency) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        minimax_channel_id,
                        "minimax-m3.1-flash-preview",
                        "normal",
                        "全天",
                        0.0,
                        0.0,
                        "元/百万tokens",
                        "manual",
                        "minimax-m3.1-flash-preview",
                        "CNY",
                    ),
                )
                print(f"  [OK] 新增价行: {NEW_MODEL} @ minimax_compat")
            if del_rows:
                conn.execute(
                    f"DELETE FROM price_snapshots "
                    f"WHERE model IN ({placeholders}) AND channel_id = ?",
                    (*REMOVED_MODELS, minimax_channel_id),
                )
                print(f"  [OK] 移除价行: {len(del_rows)} 个老模型 @ minimax_compat")
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
