"""V6.14 migration: minimax-m3.1-flash-preview 入 tier:vision 组, 优先级高于 minimax-m3 (2026-09-27)

背景 (用户拍板 2026-09-27):
- minimax-m3.1-flash-preview (V6.14 上午接入, 多模态 preview) 加入 tier:vision 组
- 要求: 优先于 minimax-m3 使用 → member_priority 越小越优先 (V5.1 排序键
  (price, family_bonus, member_priority, -channel.priority), 同价时生效)
- 现状 (生产 2026-09-27): tier:vision 活跃成员 minimax-m3 (priority=100) +
  deepseek-flash (120, modelscope 0 元兜底); m3/m3.1 价行均 0 元同渠道,
  priority 直接决定位次 → m3.1 取 90: m3.1(90) → m3(100) → deepseek-flash(120)
- 过滤条件核对: tier=None ✓ / require_vision=1 且 registry modality=multimodal ✓ /
  min_context=0 ✓ / capabilities=None ✓ / 规范名价行存在 (minimax_compat) ✓
- 纯文本请求 P0-3 跳过 multimodal 成员属既有设计 (2026-09-18 拍板不变)

改动:
1. 确保 (tier:vision, minimax-m3.1-flash-preview) 成员行: 缺失则 INSERT(priority=90),
   软删除行则复活+改 90, 活跃行 priority≠90 则对齐 90 (幂等收敛到目标态)

用法:
    python db/migrations/2026_09_27_v6x_minimax_m31_vision_group.py            # dry-run
    python db/migrations/2026_09_27_v6x_minimax_m31_vision_group.py --apply   # 实际执行
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

GROUP_ID = "tier:vision"
MODEL_ID = "minimax-m3.1-flash-preview"
TARGET_PRIORITY = 90


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

    # ============ 0. 前置检查 ============
    grp = conn.execute(
        "SELECT id, group_id, require_vision, enabled FROM model_groups WHERE group_id = ?",
        (GROUP_ID,),
    ).fetchone()
    if not grp:
        print(f"[FAIL] 组 {GROUP_ID} 不存在")
        conn.close()
        return
    print(f"组: {grp['group_id']} require_vision={grp['require_vision']} enabled={grp['enabled']}")

    reg = conn.execute(
        "SELECT model_id, modality, status FROM model_registry WHERE model_id = ?",
        (MODEL_ID,),
    ).fetchone()
    if not reg:
        print(f"[FAIL] {MODEL_ID} 不在 model_registry (先跑 2026_09_27_v6x_minimax_m31_flash_preview.py)")
        conn.close()
        return
    print(f"registry: {reg['model_id']} modality={reg['modality']} status={reg['status']}")
    if reg["modality"] != "multimodal":
        print(f"[FAIL] modality={reg['modality']}, require_vision 组会拒收, 中止")
        conn.close()
        return

    # ============ 1. 现状 ============
    print(f"\n=== {GROUP_ID} 活跃成员现状 (priority 越小越优先) ===")
    for r in conn.execute(
        "SELECT model_id, priority, added_by FROM model_group_members "
        "WHERE group_id = ? AND is_active = 1 ORDER BY priority",
        (GROUP_ID,),
    ):
        print(f"  {r['model_id']:32s} priority={r['priority']} added_by={r['added_by']}")

    row = conn.execute(
        "SELECT id, is_active, priority, added_by FROM model_group_members "
        "WHERE group_id = ? AND model_id = ?",
        (GROUP_ID, MODEL_ID),
    ).fetchone()

    action = None
    if row and row["is_active"] == 1:
        if row["priority"] == TARGET_PRIORITY:
            print(f"\n[OK] 成员行已存在且 priority={TARGET_PRIORITY}, 无需操作")
            conn.close()
            return
        action = "UPDATE"
        print(f"\n[TO-UPDATE] 活跃成员行 priority {row['priority']} → {TARGET_PRIORITY}")
    elif row:
        action = "REACTIVATE"
        print(f"\n[TO-REACTIVATE] 软删除行 id={row['id']} 复活 + priority={TARGET_PRIORITY} "
              f"(清 removed_at/reason/by)")
    else:
        action = "INSERT"
        print(f"\n[TO-INSERT] 新成员行 ({GROUP_ID}, {MODEL_ID}, priority={TARGET_PRIORITY})")

    if not args.apply:
        print("\n[DRY-RUN] 加 --apply 才执行")
        conn.close()
        return

    print("\n[APPLY] 开始迁移...")
    try:
        conn.execute("BEGIN")
        if action == "INSERT":
            conn.execute(
                "INSERT INTO model_group_members "
                "(group_id, model_id, priority, weight, added_by, is_active) "
                "VALUES (?, ?, ?, 1.0, 'manual', 1)",
                (GROUP_ID, MODEL_ID, TARGET_PRIORITY),
            )
        elif action == "REACTIVATE":
            conn.execute(
                "UPDATE model_group_members SET is_active = 1, priority = ?, "
                "added_by = 'manual', removed_at = NULL, removed_reason = NULL, "
                "removed_by = NULL WHERE id = ?",
                (TARGET_PRIORITY, row["id"]),
            )
        else:
            conn.execute(
                "UPDATE model_group_members SET priority = ? WHERE id = ?",
                (TARGET_PRIORITY, row["id"]),
            )
        conn.execute("COMMIT")
        print(f"  [OK] {action}: {MODEL_ID} @ {GROUP_ID} priority={TARGET_PRIORITY}")
    except Exception as e:
        conn.execute("ROLLBACK")
        print(f"[FAIL] 迁移失败: {e}")
        raise

    print(f"\n=== 迁移后 {GROUP_ID} 活跃成员 (期望顺序) ===")
    for r in conn.execute(
        "SELECT model_id, priority FROM model_group_members "
        "WHERE group_id = ? AND is_active = 1 ORDER BY priority",
        (GROUP_ID,),
    ):
        print(f"  {r['model_id']:32s} priority={r['priority']}")

    conn.close()


if __name__ == "__main__":
    main()
