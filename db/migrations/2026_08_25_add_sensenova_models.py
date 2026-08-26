"""V4.9.5 migration: 接入 SenseNova 模型 (2026-08-25)

背景:
- 用户接受 SenseNova 模型评估推荐:
  1. sensenova-6.8-flash-lite 加入 model_registry + tier:balanced 组 (modality=text)
  2. glm-5.2 加入 sensenova_official 的 price_snapshots (免费第二渠道)
- VM 上不能直接跑 seed 脚本 (delete-then-insert 会丢失 siliconflow sync 的 91 个模型)
- 用本迁移脚本精确 INSERT, 幂等检查, 不影响已有数据

改动:
1. INSERT sensenova-6.8-flash-lite 到 model_registry (if not exists)
2. INSERT sensenova-6.8-flash-lite 到 model_group_members (tier:balanced, priority=120)
3. INSERT glm-5.2 price_snapshot 到 sensenova_official 渠道 (if not exists)
4. INSERT sensenova-6.8-flash-lite price_snapshot 到 sensenova_official 渠道 (if not exists)

用法:
    python db/migrations/2026_08_25_add_sensenova_models.py            # dry-run
    python db/migrations/2026_08_25_add_sensenova_models.py --apply   # 实际执行
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


# ===== 数据定义 =====

# sensenova-6.8-flash-lite — model_registry 条目
# 权威来源: scripts/seed_model_registry.py
REGISTRY_ENTRY = {
    "model_id": "sensenova-6.8-flash-lite",
    "family": "sensenova",
    "display_name": "SenseNova 6.8 Flash Lite",
    "tier": "T2",
    "capabilities": '["reasoning", "general"]',
    "context_length": 262144,
    "modality": "text",
    "is_open_source": 0,
    "release_date": "2026-08",
    "status": "active",
    "notes": "商汤日日新公测, 256K ctx, 支持 tools/vision, price=0.0 (公测免费)",
}

# tier:balanced 组成员
# 权威来源: scripts/seed_model_groups.py
GROUP_MEMBER = {
    "group_id": "tier:balanced",
    "model_id": "sensenova-6.8-flash-lite",
    "priority": 120,
    "weight": 1.0,
    "max_cost_ratio": None,
    "added_by": "manual",
}

# price_snapshots — sensenova_official 渠道上的新条目
# glm-5.2: 跨渠道免费第二渠道 (cheapest 策略下 0.0 胜过 siliconflow 8.0)
# sensenova-6.8-flash-lite: 公测主力 (fix_sensenova_models.py 可能已加过, 幂等)
PRICE_SNAPSHOTS_TO_ADD = [
    ("glm-5.2", "normal", "全天", 0.0, 0.0),
    ("sensenova-6.8-flash-lite", "normal", "全天", 0.0, 0.0),
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    db_path = get_db_path()
    print(f"db path: {db_path}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # ============ 0. 查 sensenova channel id ============
    ch_row = conn.execute(
        "SELECT id, name, provider FROM channels WHERE provider = 'sensenova'"
    ).fetchone()
    if not ch_row:
        print("[FAIL] sensenova 渠道不存在, 无法继续")
        conn.close()
        return
    sensenova_channel_id = ch_row["id"]
    print(f"sensenova 渠道: id={sensenova_channel_id}, name={ch_row['name']}")

    # ============ 1. 检查 model_registry ============
    print("\n=== 1. model_registry 检查 ===")
    reg_row = conn.execute(
        "SELECT model_id, family, tier, modality FROM model_registry "
        "WHERE model_id = ?",
        (REGISTRY_ENTRY["model_id"],),
    ).fetchone()

    actions = []  # (description, sql, params)

    if reg_row:
        print(f"  [EXISTS] {reg_row['model_id']} family={reg_row['family']} "
              f"tier={reg_row['tier']} modality={reg_row['modality']}")
    else:
        print(f"  [INSERT] {REGISTRY_ENTRY['model_id']} "
              f"family={REGISTRY_ENTRY['family']} tier={REGISTRY_ENTRY['tier']} "
              f"modality={REGISTRY_ENTRY['modality']}")
        actions.append((
            f"INSERT model_registry: {REGISTRY_ENTRY['model_id']}",
            """INSERT INTO model_registry
               (model_id, family, display_name, tier, capabilities,
                context_length, modality, is_open_source, release_date,
                status, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                REGISTRY_ENTRY["model_id"],
                REGISTRY_ENTRY["family"],
                REGISTRY_ENTRY["display_name"],
                REGISTRY_ENTRY["tier"],
                REGISTRY_ENTRY["capabilities"],
                REGISTRY_ENTRY["context_length"],
                REGISTRY_ENTRY["modality"],
                REGISTRY_ENTRY["is_open_source"],
                REGISTRY_ENTRY["release_date"],
                REGISTRY_ENTRY["status"],
                REGISTRY_ENTRY["notes"],
            ),
        ))

    # ============ 2. 检查 model_group_members ============
    print("\n=== 2. model_group_members 检查 ===")
    gm_row = conn.execute(
        "SELECT group_id, model_id, priority, is_active FROM model_group_members "
        "WHERE group_id = ? AND model_id = ?",
        (GROUP_MEMBER["group_id"], GROUP_MEMBER["model_id"]),
    ).fetchone()

    if gm_row:
        print(f"  [EXISTS] {gm_row['group_id']}/{gm_row['model_id']} "
              f"priority={gm_row['priority']} is_active={gm_row['is_active']}")
    else:
        print(f"  [INSERT] {GROUP_MEMBER['group_id']}/{GROUP_MEMBER['model_id']} "
              f"priority={GROUP_MEMBER['priority']}")
        actions.append((
            f"INSERT model_group_members: {GROUP_MEMBER['group_id']}/{GROUP_MEMBER['model_id']}",
            """INSERT INTO model_group_members
               (group_id, model_id, priority, weight, max_cost_ratio,
                added_by, is_active)
               VALUES (?, ?, ?, ?, ?, ?, 1)""",
            (
                GROUP_MEMBER["group_id"],
                GROUP_MEMBER["model_id"],
                GROUP_MEMBER["priority"],
                GROUP_MEMBER["weight"],
                GROUP_MEMBER["max_cost_ratio"],
                GROUP_MEMBER["added_by"],
            ),
        ))

    # ============ 3. 检查 price_snapshots ============
    print("\n=== 3. price_snapshots 检查 (sensenova_official) ===")
    for model, price_type, time_range, input_price, output_price in PRICE_SNAPSHOTS_TO_ADD:
        ps_row = conn.execute(
            "SELECT id, model, input_price, output_price FROM price_snapshots "
            "WHERE channel_id = ? AND model = ? AND price_type = ? AND time_range = ?",
            (sensenova_channel_id, model, price_type, time_range),
        ).fetchone()

        if ps_row:
            print(f"  [EXISTS] model={model} price_type={price_type} "
                  f"input={ps_row['input_price']} output={ps_row['output_price']}")
        else:
            print(f"  [INSERT] model={model} price_type={price_type} "
                  f"input={input_price} output={output_price}")
            actions.append((
                f"INSERT price_snapshot: {model}",
                """INSERT INTO price_snapshots
                   (channel_id, model, price_type, time_range,
                    input_price, output_price, unit, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    sensenova_channel_id,
                    model,
                    price_type,
                    time_range,
                    input_price,
                    output_price,
                    "元/百万tokens",
                    "manual",
                ),
            ))

    # ============ 4. 汇总 ============
    if not actions:
        print("\n[OK] 所有数据已存在, 无需操作")
        conn.close()
        return

    print(f"\n共 {len(actions)} 项操作待执行:")
    for desc, _, _ in actions:
        print(f"  - {desc}")

    if not args.apply:
        print("\n[DRY-RUN] 加 --apply 才执行")
        conn.close()
        return

    # ============ 5. 执行 ============
    print("\n[APPLY] 开始执行...")
    try:
        conn.execute("BEGIN")
        for desc, sql, params in actions:
            conn.execute(sql, params)
            print(f"  [OK] {desc}")
        conn.execute("COMMIT")
        print(f"\n[OK] 完成 {len(actions)} 项操作")
    except Exception as e:
        conn.execute("ROLLBACK")
        print(f"[FAIL] 执行失败: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
