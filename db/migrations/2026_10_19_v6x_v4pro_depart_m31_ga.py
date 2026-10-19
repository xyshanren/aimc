"""V6.x migration: deepseek-v4-pro 出组 flagship + minimax-m3.1-flash GA (2026-10-19, 用户拍板)

背景:
- DeepSeek V4.1-Flash-1017 上线 (2026-10-17, 官方模型名仍 deepseek-flash),
  deepseek-flash 与 v4-pro 彻底拉开差距 → v4-pro 出 flagship 组;
  registry + ch1/modelscope 价行保留, 精确直连不受影响
- MiniMax M3.1 Flash 正式 GA (2026-10-19): 去 preview 后缀, 早期以 Space Bunny
  匿名灰度 (200T token); 官方公布推理提速近 3 倍 (日常 200+ TPS, P50 170+),
  Agentic 综合成本降 70%; 标价 1 / 4 / cache 0.1 元每百万tokens
- 用户拍板: tier T1 不变, tier:vision(90) → tier:strong(100) 首位,
  待用一段时间后再评估是否升组

改动 (幂等收敛):
1. flagship: deepseek-v4-pro 软删除 (is_active=0 + 审计字段)
2. model_registry: minimax-m3.1-flash-preview 改名 minimax-m3.1-flash
   (display_name/notes 同步, tier 对齐 T1, list_input_price=1.0 /
   list_output_price=4.0 官方标价双轨回填 — 成本审计用, 不参与路由)
3. price_snapshots: minimax_compat 渠道改名 (model + raw_model_id);
   价行保持 0.0/0.0 — M Plan 订阅边际成本, cheapest 策略下填标价
   会被同组 0 元成员反压 priority
4. model_group_members: vision 行软删除 (改名后留档, reason=m31_ga_move_to_strong);
   strong 行改名 + priority 收敛 100 (幂等: 已存在则收敛 model_id+priority)

⚠️ 不在本迁移范围: 生产 .env AIMC_PARSE_MODEL 若指向 minimax-m3.1-flash-preview
   需同步改为 minimax-m3.1-flash (parse 走精确路由); 迁移后需刷新路由缓存/重启

用法:
    python db/migrations/2026_10_19_v6x_v4pro_depart_m31_ga.py            # dry-run
    python db/migrations/2026_10_19_v6x_v4pro_depart_m31_ga.py --apply   # 实际执行
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

OLD_MODEL = "minimax-m3.1-flash-preview"
NEW_MODEL = "minimax-m3.1-flash"
DEPART_MEMBER = ("tier:flagship", "deepseek-v4-pro")
STRONG_PRIORITY = 100
LIST_INPUT_PRICE = 1.0   # 官方标价 元/百万tokens (2026-10-19 官方发布)
LIST_OUTPUT_PRICE = 4.0
NOW = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

NEW_NOTES = (
    "M3.1 Flash 正式版 (2026-10-19 GA, 原 m3.1-flash-preview), 多模态, 1M ctx; "
    "官方标价 1/4 cache 0.1 元每百万tokens; tier:strong 成员 (priority=100 首位)"
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

    # ============ 1. flagship deepseek-v4-pro 出组 ============
    print("\n=== 1. flagship deepseek-v4-pro 出组检查 ===")
    dep = conn.execute(
        "SELECT id, is_active, priority FROM model_group_members "
        "WHERE group_id = ? AND model_id = ?",
        DEPART_MEMBER,
    ).fetchone()
    dep_action = None
    if not dep:
        print("  [NONE] 无成员行, 无需处理")
    elif dep["is_active"] == 0:
        print("  [OK] 已软删除 (is_active=0)")
    else:
        dep_action = dep["id"]
        print(f"  [TO-DEACTIVATE] id={dep['id']} priority={dep['priority']} 软删除")

    # ============ 2. model_registry 改名 ============
    print(f"\n=== 2. model_registry 改名检查 ({OLD_MODEL} → {NEW_MODEL}) ===")
    old_reg = conn.execute(
        "SELECT model_id, tier, status, list_input_price, list_output_price "
        "FROM model_registry WHERE model_id = ?",
        (OLD_MODEL,),
    ).fetchone()
    new_reg = conn.execute(
        "SELECT model_id FROM model_registry WHERE model_id = ?",
        (NEW_MODEL,),
    ).fetchone()
    reg_action = None
    if new_reg:
        print(f"  [OK] {NEW_MODEL} 已存在, 无需改名")
    elif not old_reg:
        print("  [FAIL] 旧行不存在且新行不存在, 中止 (m3.1 未接入?)")
        conn.close()
        return
    else:
        reg_action = old_reg["model_id"]
        print(f"  [TO-RENAME] {OLD_MODEL} (tier={old_reg['tier']} "
              f"list_price={old_reg['list_input_price']}/{old_reg['list_output_price']}) "
              f"→ {NEW_MODEL} (tier=T1, list_price=1.0/4.0)")

    # ============ 3. price_snapshots 改名 (仅 minimax_compat) ============
    print("\n=== 3. price_snapshots 改名检查 (minimax_compat) ===")
    ch_row = conn.execute(
        "SELECT id, name FROM channels WHERE provider = 'minimax'"
    ).fetchone()
    if not ch_row:
        print("  [FAIL] minimax 渠道不存在, 中止")
        conn.close()
        return
    minimax_channel_id = ch_row["id"]
    ps_old = conn.execute(
        "SELECT id, input_price, output_price, raw_model_id FROM price_snapshots "
        "WHERE channel_id = ? AND model = ?",
        (minimax_channel_id, OLD_MODEL),
    ).fetchall()
    ps_new = conn.execute(
        "SELECT id FROM price_snapshots WHERE channel_id = ? AND model = ?",
        (minimax_channel_id, NEW_MODEL),
    ).fetchone()
    ps_rename = []
    if ps_new:
        print(f"  [OK] {NEW_MODEL} 价行已存在, 无需改名")
    else:
        for r in ps_old:
            ps_rename.append(r["id"])
            print(f"  [TO-RENAME] id={r['id']} {r['input_price']}/{r['output_price']} "
                  f"raw={r['raw_model_id']} → {NEW_MODEL}")

    # ============ 4. 组成员: vision 软删 + strong 新增 ============
    print("\n=== 4. 组成员检查 (vision 软删 + strong 入组) ===")
    vis_row = conn.execute(
        "SELECT id, is_active, model_id FROM model_group_members "
        "WHERE group_id = 'tier:vision' AND model_id IN (?, ?)",
        (OLD_MODEL, NEW_MODEL),
    ).fetchone()
    vis_action = None
    if not vis_row:
        print("  [NONE] vision 无成员行, 无需处理")
    elif vis_row["is_active"] == 0:
        print("  [OK] vision 行已软删除")
    else:
        vis_action = vis_row["id"]
        print(f"  [TO-DEACTIVATE] vision id={vis_row['id']} ({vis_row['model_id']}) 软删除")

    strong_row = conn.execute(
        "SELECT id, model_id, priority, is_active FROM model_group_members "
        "WHERE group_id = 'tier:strong' AND model_id IN (?, ?)",
        (OLD_MODEL, NEW_MODEL),
    ).fetchone()
    strong_insert = strong_update = None
    if not strong_row:
        strong_insert = True
        print(f"  [TO-INSERT] tier:strong <- {NEW_MODEL} priority={STRONG_PRIORITY}")
    elif (strong_row["is_active"] == 1 and strong_row["priority"] == STRONG_PRIORITY
          and strong_row["model_id"] == NEW_MODEL):
        print(f"  [OK] strong 行已就绪 priority={STRONG_PRIORITY}")
    else:
        strong_update = strong_row["id"]
        if strong_row["model_id"] != NEW_MODEL:
            print(f"  [TO-UPDATE] strong id={strong_row['id']}: model_id {strong_row['model_id']} "
                  f"→ {NEW_MODEL}, priority={strong_row['priority']} → {STRONG_PRIORITY}")
        else:
            print(f"  [TO-UPDATE] strong id={strong_row['id']}: "
                  f"priority={strong_row['priority']} → {STRONG_PRIORITY}")

    # ============ 5. 信息项: 迁移后组态预览 ============
    print("\n=== 5. [INFO] 当前 flagship / strong / vision 活跃成员 ===")
    for gid in ("tier:flagship", "tier:strong", "tier:vision"):
        rows = conn.execute(
            "SELECT model_id, priority FROM model_group_members "
            "WHERE group_id = ? AND is_active = 1 ORDER BY priority",
            (gid,),
        ).fetchall()
        members = ", ".join(f"{r['model_id']}({r['priority']})" for r in rows) or "(空)"
        print(f"  {gid}: {members}")

    pending = bool(dep_action or reg_action or ps_rename or vis_action
                   or strong_insert or strong_update)
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
        if dep_action:
            conn.execute(
                "UPDATE model_group_members SET is_active = 0, removed_at = ?, "
                "removed_reason = 'manual', removed_by = 'manual' WHERE id = ?",
                (NOW, dep_action),
            )
            print(f"  [OK] flagship <- deepseek-v4-pro 软删除 (V4.1-Flash-1017 后拉开差距)")
        if reg_action:
            conn.execute(
                "UPDATE model_registry SET model_id = ?, display_name = 'MiniMax M3.1 Flash', "
                "tier = 'T1', notes = ?, list_input_price = ?, list_output_price = ? "
                "WHERE model_id = ?",
                (NEW_MODEL, NEW_NOTES, LIST_INPUT_PRICE, LIST_OUTPUT_PRICE, OLD_MODEL),
            )
            print(f"  [OK] registry 改名 {OLD_MODEL} → {NEW_MODEL} (tier=T1, "
                  f"list_price={LIST_INPUT_PRICE}/{LIST_OUTPUT_PRICE})")
        for rid in ps_rename:
            conn.execute(
                "UPDATE price_snapshots SET model = ?, raw_model_id = ? WHERE id = ?",
                (NEW_MODEL, NEW_MODEL, rid),
            )
            print(f"  [OK] 价行 id={rid} 改名 → {NEW_MODEL} (0.0 保持)")
        if vis_action:
            conn.execute(
                "UPDATE model_group_members SET is_active = 0, removed_at = ?, "
                "removed_reason = 'm31_ga_move_to_strong', removed_by = 'manual' "
                "WHERE id = ?",
                (NOW, vis_action),
            )
            print(f"  [OK] vision 行软删除 (迁入 strong)")
        if strong_insert:
            conn.execute(
                "INSERT INTO model_group_members "
                "(group_id, model_id, priority, weight, added_by, is_active, created_at) "
                "VALUES ('tier:strong', ?, ?, 1.0, 'manual', 1, ?)",
                (NEW_MODEL, STRONG_PRIORITY, NOW),
            )
            print(f"  [OK] strong <- {NEW_MODEL} priority={STRONG_PRIORITY}")
        elif strong_update:
            conn.execute(
                "UPDATE model_group_members SET model_id = ?, priority = ?, "
                "is_active = 1, removed_at = NULL, removed_reason = NULL WHERE id = ?",
                (NEW_MODEL, STRONG_PRIORITY, strong_update),
            )
            print(f"  [OK] strong 行收敛 → {NEW_MODEL} priority={STRONG_PRIORITY} (active)")
        conn.execute("COMMIT")
        print("\n[OK] 迁移完成")
    except Exception as e:
        conn.execute("ROLLBACK")
        print(f"[FAIL] 迁移失败: {e}")
        raise

    print("\n=== 迁移后活跃成员 (期望顺序) ===")
    for gid in ("tier:flagship", "tier:strong", "tier:vision"):
        rows = conn.execute(
            "SELECT model_id, priority FROM model_group_members "
            "WHERE group_id = ? AND is_active = 1 ORDER BY priority",
            (gid,),
        ).fetchall()
        members = ", ".join(f"{r['model_id']}({r['priority']})" for r in rows) or "(空)"
        print(f"  {gid}: {members}")

    conn.close()


if __name__ == "__main__":
    main()
