"""V6.14 migration: tier:flagship / tier:balanced 组成员重排 (2026-09-27, 用户拍板)

背景:
- flagship 现状 (生产 2026-09-27): v4-pro(100) / kimi-k3(105) / glm-5.3(110) /
  deepseek-flash(115), 四成员均有 0 元供给, 同价由 member_priority 决位 → v4-pro 首位
- 用户要求: deepseek-flash 提到首位; v4-pro 排第三; glm-5.3 排最后 (kimi-k3 第二不动)
  新位次: flash(100) → kimi(105) → v4-pro(110) → glm-5.3(115)
  注: v4-pro 0 元供给已因 sensenova 退役只剩 modelscope; flash 有 modelscope +
  sensenova 双 0 元 + ch1 官方分时段付费兜底 (1/4 谷 + 2/8 峰, cached 0.02/0.04)
- balanced 现状: deepseek-v3(100, SF 付费 2/8 组内唯一付费) / minimax-m3(110) /
  sensenova-6.8-flash-lite(120)
- 用户要求: deepseek-v3 出组 (软删除); sensenova-6.8-flash-lite 提到首位
  新位次: 6.8-flash-lite(100) → minimax-m3(110 不变)
- 解析模型临时切换 (AIMC_PARSE_MODEL=minimax-m3.1-flash-preview) 走生产 .env,
  不在本迁移范围

改动 (仅 model_group_members, 幂等收敛):
1. flagship 四行 priority 对齐新位次
2. balanced: deepseek-v3 软删除 (is_active=0 + removed_at/reason/by);
   6.8-flash-lite priority 对齐 100

用法:
    python db/migrations/2026_09_27_v6x_group_priority_rebalance.py            # dry-run
    python db/migrations/2026_09_27_v6x_group_priority_rebalance.py --apply   # 实际执行
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

# (group_id, model_id, target_priority)
PRIORITY_TARGETS = (
    ("tier:flagship", "deepseek-flash", 100),
    ("tier:flagship", "kimi-k3", 105),          # 不变, 幂等项
    ("tier:flagship", "deepseek-v4-pro", 110),
    ("tier:flagship", "glm-5.3", 115),
    ("tier:balanced", "sensenova-6.8-flash-lite", 100),
    ("tier:balanced", "minimax-m3", 110),        # 不变, 幂等项
)
# 出组: 软删除
DEPART = ("tier:balanced", "deepseek-v3")


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

    # ============ 1. priority 收敛清单 ============
    updates = []
    print("\n=== 1. priority 重排检查 ===")
    for gid, mid, target in PRIORITY_TARGETS:
        row = conn.execute(
            "SELECT id, priority, is_active FROM model_group_members "
            "WHERE group_id = ? AND model_id = ?",
            (gid, mid),
        ).fetchone()
        if not row:
            print(f"  [FAIL] {gid} <- {mid} 无成员行, 中止")
            conn.close()
            return
        if row["is_active"] != 1:
            print(f"  [FAIL] {gid} <- {mid} 非活跃 (is_active=0), 中止")
            conn.close()
            return
        if row["priority"] == target:
            print(f"  [OK] {gid} <- {mid} priority={target} (已就绪)")
        else:
            updates.append((gid, mid, target, row["id"], row["priority"]))
            print(f"  [TO-UPDATE] {gid} <- {mid}: {row['priority']} → {target}")

    # ============ 2. 出组检查 ============
    print("\n=== 2. 出组检查 (deepseek-v3 @ balanced) ===")
    dep = conn.execute(
        "SELECT id, is_active, removed_at FROM model_group_members "
        "WHERE group_id = ? AND model_id = ?",
        DEPART,
    ).fetchone()
    dep_action = None
    if not dep:
        print("  [NONE] 无成员行, 无需处理")
    elif dep["is_active"] == 0:
        print("  [OK] 已软删除 (is_active=0)")
    else:
        dep_action = dep["id"]
        print(f"  [TO-DEACTIVATE] id={dep['id']} 软删除 (is_active=0 + 审计字段)")

    # ============ 3. 信息项: deepseek-flash 分时段价行 (用户提示核对) ============
    print("\n=== 3. [INFO] deepseek-flash 全渠道价行 (含时段) ===")
    for r in conn.execute("""
        SELECT c.name, ps.time_range, ps.input_price, ps.output_price, ps.cached_input_price
        FROM price_snapshots ps JOIN channels c ON c.id = ps.channel_id
        WHERE ps.model = 'deepseek-flash' ORDER BY c.name, ps.input_price"""):
        print(f"  {r['name']:24s} [{r['time_range']}] "
              f"{r['input_price']}/{r['output_price']} cache={r['cached_input_price']}")

    if not updates and dep_action is None:
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
        for gid, mid, target, rid, old in updates:
            conn.execute(
                "UPDATE model_group_members SET priority = ? WHERE id = ?",
                (target, rid),
            )
            print(f"  [OK] {gid} <- {mid}: {old} → {target}")
        if dep_action:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            conn.execute(
                "UPDATE model_group_members SET is_active = 0, removed_at = ?, "
                "removed_reason = 'manual', removed_by = 'manual' WHERE id = ?",
                (now, dep_action),
            )
            print(f"  [OK] balanced <- deepseek-v3 软删除")
        conn.execute("COMMIT")
        print("\n[OK] 迁移完成")
    except Exception as e:
        conn.execute("ROLLBACK")
        print(f"[FAIL] 迁移失败: {e}")
        raise

    for gid in ("tier:flagship", "tier:balanced"):
        print(f"\n=== 迁移后 {gid} 活跃成员 (期望顺序) ===")
        for r in conn.execute(
            "SELECT model_id, priority FROM model_group_members "
            "WHERE group_id = ? AND is_active = 1 ORDER BY priority",
            (gid,),
        ):
            print(f"  {r['model_id']:32s} priority={r['priority']}")

    conn.close()


if __name__ == "__main__":
    main()
