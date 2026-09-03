"""V6.0 migration: GLM 免费模型接入 + vision 分组重排 (2026-09-03)

背景 (docs/next-major-v6-plan.md §1.2/§1.4, 设计已用户确认):
- light 补免费车道: glm-4.7-flash (bigmodel 文本免费) 入 tier:light
- vision 旗舰收缩: deepseek-v4-flash-vision-exp + glm-5.3-flash (多模态补位);
  glm-4.5v (上一代付费) 直接移除; qwen-vl 系列下沉轻量视觉组
- 新建 tier:vision-light: glm-4.6v-flash (视觉免费) + qwen-vl 系列
- fallback 链: tier:vision → tier:vision-light → tier:light

VM 上不能直接跑 seed 脚本 (delete-then-insert 会丢 siliconflow sync 的模型
+ 审计历史), 用本迁移精确变更, 幂等, 不影响已有数据。

改动:
1. model_registry: glm-5.3-flash modality text→multimodal;
   INSERT glm-4.7-flash / glm-4.6v-flash (if not exists)
2. model_groups: tier:vision tier→NULL + fallback→vision-light;
   INSERT tier:vision-light (if not exists)
3. model_group_members:
   - tier:vision 软删 qwen-vl×4 + glm-4.5v (保留审计)
   - tier:vision + glm-5.3-flash (reactivate or insert)
   - tier:vision-light + 5 成员
   - tier:light + glm-4.7-flash
4. price_snapshots: glm_bigmodel 渠道补 glm-4.7-flash / glm-4.6v-flash
   0 元行 (coding plan 订阅制不计价, 与现有 glm-5.3-flash 0.0 口径一致)

前置: gateway/router.py P0-3 已收窄 (仅 require_vision 组内跳过 multimodal),
否则 glm-5.3-flash multimodal 化后 strong/code 文本请求会跳过它。

安全 (Mimosa): SQL 全部为 execute 调用点内联字面量 + ? 占位符参数,
值永远走 params, 无任何拼接。

用法:
    python db/migrations/2026_09_03_v60_glm_flash_vision.py            # dry-run
    python db/migrations/2026_09_03_v60_glm_flash_vision.py --apply   # 实际执行
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))


def get_db_path() -> str:
    import config
    db_path = config.settings.db_path
    if db_path == ":memory:":
        return "/tmp/test_aimc.db"
    return db_path


# ===== 数据定义 (权威来源: scripts/seed_model_registry.py / seed_model_groups.py) =====

GLM_53_FLASH = "glm-5.3-flash"
GLM_47_FLASH = "glm-4.7-flash"
GLM_46V_FLASH = "glm-4.6v-flash"
VISION_FALLBACK_NEW = "tier:vision-light"
REMOVE_REASON = "v60_vision_regroup (glm-4.5v 移除 / qwen-vl 下沉 vision-light)"

# (model_id, family, display_name, tier, capabilities, context_length,
#  modality, is_open_source, release_date, status, notes)
REGISTRY_TO_ADD = [
    (
        GLM_47_FLASH, "glm", "GLM-4.7 Flash", "T3",
        '["general", "code"]', 200000, "text", 0, "2026-09", "active",
        "GLM-4.7 极速版, bigmodel 免费 (输入/输出/缓存全免费); "
        "常态 1305 过载, 靠 failover 兜底",
    ),
    (
        GLM_46V_FLASH, "glm", "GLM-4.6V Flash", "T3",
        '["vision", "general"]', 131072, "vision", 0, "2026-09", "active",
        "GLM-4.6V 极速视觉, bigmodel 免费; 接受图片/视频/文件/文本; "
        "注意与付费 GLM-4.6V (1/3元) 是不同模型",
    ),
]

VISION_LIGHT_GROUP = (
    "tier:vision-light", "轻量视觉组", None, None, 0, 1, "cheapest",
    "tier:light", 26,
)

# tier:vision 软删 (保留审计行)
MEMBERS_TO_REMOVE_FROM_VISION = [
    "qwen-vl-32b-instruct",
    "qwen-vl-32b-thinking",
    "qwen-vl-30b-a3b-thinking",
    "qwen-vl-8b-instruct",
    "glm-4.5v",
]

# (group_id, model_id, priority)
MEMBERS_TO_ADD = [
    ("tier:vision", GLM_53_FLASH, 110),
    ("tier:vision-light", GLM_46V_FLASH, 100),
    ("tier:vision-light", "qwen-vl-32b-instruct", 110),
    ("tier:vision-light", "qwen-vl-32b-thinking", 120),
    ("tier:vision-light", "qwen-vl-30b-a3b-thinking", 130),
    ("tier:vision-light", "qwen-vl-8b-instruct", 140),
    ("tier:light", GLM_47_FLASH, 130),
]

GLM_CHANNEL_NAME = "glm_bigmodel"
# (model, price_type, time_range, input_price, output_price)
PRICE_SNAPSHOTS_TO_ADD = [
    (GLM_47_FLASH, "normal", "全天", 0.0, 0.0),
    (GLM_46V_FLASH, "normal", "全天", 0.0, 0.0),
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    db_path = get_db_path()
    print(f"db path: {db_path}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # (描述, 零参执行闭包 — SQL 字面量内联在闭包内的 execute 调用点)
    pending: list[tuple[str, Callable[[], None]]] = []

    # ============ 1. model_registry ============
    print("\n=== 1. model_registry ===")
    row = conn.execute(
        "SELECT modality FROM model_registry WHERE model_id = ?",
        (GLM_53_FLASH,),
    ).fetchone()
    if not row:
        print(f"  [SKIP] {GLM_53_FLASH} 不在 registry")
    elif row["modality"] == "multimodal":
        print(f"  [OK] {GLM_53_FLASH} modality 已是 multimodal")
    else:
        print(f"  [UPDATE] {GLM_53_FLASH} modality {row['modality']} -> multimodal")
        pending.append((
            f"registry modality: {GLM_53_FLASH}",
            lambda: conn.execute(
                "UPDATE model_registry SET modality = ? WHERE model_id = ?",
                ("multimodal", GLM_53_FLASH),
            ),
        ))

    for entry in REGISTRY_TO_ADD:
        exists = conn.execute(
            "SELECT 1 FROM model_registry WHERE model_id = ?", (entry[0],)
        ).fetchone()
        if exists:
            print(f"  [EXISTS] {entry[0]}")
            continue
        print(f"  [INSERT] {entry[0]} tier={entry[3]} modality={entry[6]}")
        pending.append((
            f"registry insert: {entry[0]}",
            lambda e=entry: conn.execute(
                "INSERT INTO model_registry"
                " (model_id, family, display_name, tier, capabilities,"
                "  context_length, modality, is_open_source, release_date,"
                "  status, notes)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                e,
            ),
        ))

    # ============ 2. model_groups ============
    print("\n=== 2. model_groups ===")
    vrow = conn.execute(
        "SELECT tier, fallback_group FROM model_groups WHERE group_id = ?",
        ("tier:vision",),
    ).fetchone()
    if not vrow:
        print("  [FAIL] tier:vision 组不存在")
        conn.close()
        return
    if vrow["tier"] is None and vrow["fallback_group"] == VISION_FALLBACK_NEW:
        print("  [OK] tier:vision tier/fallback 已是目标值")
    else:
        print(f"  [UPDATE] tier:vision: tier {vrow['tier']} -> NULL, "
              f"fallback {vrow['fallback_group']} -> {VISION_FALLBACK_NEW}")
        pending.append((
            "group tier:vision -> tier=NULL, fallback=vision-light",
            lambda: conn.execute(
                "UPDATE model_groups SET tier = ?, fallback_group = ?"
                " WHERE group_id = ?",
                (None, VISION_FALLBACK_NEW, "tier:vision"),
            ),
        ))

    vl_exists = conn.execute(
        "SELECT 1 FROM model_groups WHERE group_id = ?",
        (VISION_LIGHT_GROUP[0],),
    ).fetchone()
    if vl_exists:
        print(f"  [EXISTS] {VISION_LIGHT_GROUP[0]}")
    else:
        print(f"  [INSERT] group {VISION_LIGHT_GROUP[0]} (require_vision=1, tier=NULL)")
        pending.append((
            f"group insert: {VISION_LIGHT_GROUP[0]}",
            lambda: conn.execute(
                "INSERT INTO model_groups"
                " (group_id, display_name, tier, capabilities, min_context,"
                "  require_vision, strategy, fallback_group, enabled, sort_order)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                VISION_LIGHT_GROUP,
            ),
        ))

    # ============ 3. model_group_members ============
    print("\n=== 3. model_group_members ===")
    now = datetime.now().isoformat(sep=" ", timespec="seconds")
    for model_id in MEMBERS_TO_REMOVE_FROM_VISION:
        row = conn.execute(
            "SELECT is_active FROM model_group_members"
            " WHERE group_id = 'tier:vision' AND model_id = ?",
            (model_id,),
        ).fetchone()
        if not row:
            print(f"  [SKIP] tier:vision/{model_id} 无行")
            continue
        if row["is_active"] == 0:
            print(f"  [OK] tier:vision/{model_id} 已软删")
            continue
        print(f"  [SOFT-DELETE] tier:vision/{model_id}")
        pending.append((
            f"member soft-delete: tier:vision/{model_id}",
            lambda m=model_id, ts=now: conn.execute(
                "UPDATE model_group_members"
                " SET is_active = 0, removed_at = ?, removed_reason = ?,"
                "     removed_by = 'manual'"
                " WHERE group_id = 'tier:vision' AND model_id = ? AND is_active = 1",
                (ts, REMOVE_REASON, m),
            ),
        ))

    for group_id, model_id, priority in MEMBERS_TO_ADD:
        row = conn.execute(
            "SELECT is_active, priority FROM model_group_members"
            " WHERE group_id = ? AND model_id = ?",
            (group_id, model_id),
        ).fetchone()
        if row and row["is_active"] == 1:
            print(f"  [EXISTS] {group_id}/{model_id} priority={row['priority']}")
            continue
        if row:
            print(f"  [REACTIVATE] {group_id}/{model_id} priority={priority}")
            pending.append((
                f"member reactivate: {group_id}/{model_id}",
                lambda g=group_id, m=model_id, p=priority: conn.execute(
                    "UPDATE model_group_members"
                    " SET is_active = 1, priority = ?, removed_at = NULL,"
                    "     removed_reason = NULL, removed_by = NULL"
                    " WHERE group_id = ? AND model_id = ?",
                    (p, g, m),
                ),
            ))
        else:
            print(f"  [INSERT] {group_id}/{model_id} priority={priority}")
            pending.append((
                f"member insert: {group_id}/{model_id}",
                lambda g=group_id, m=model_id, p=priority: conn.execute(
                    "INSERT INTO model_group_members"
                    " (group_id, model_id, priority, weight, max_cost_ratio,"
                    "  added_by, is_active)"
                    " VALUES (?, ?, ?, 1.0, NULL, 'manual', 1)",
                    (g, m, p),
                ),
            ))

    # ============ 4. price_snapshots ============
    print("\n=== 4. price_snapshots (glm_bigmodel) ===")
    ch_row = conn.execute(
        "SELECT id FROM channels WHERE name = ?", (GLM_CHANNEL_NAME,)
    ).fetchone()
    if not ch_row:
        print(f"  [FAIL] 渠道 {GLM_CHANNEL_NAME} 不存在, 跳过快照")
        conn.close()
        return
    glm_channel_id = ch_row["id"]
    for model, price_type, time_range, input_price, output_price in PRICE_SNAPSHOTS_TO_ADD:
        ps = conn.execute(
            "SELECT id FROM price_snapshots"
            " WHERE channel_id = ? AND model = ? AND price_type = ? AND time_range = ?",
            (glm_channel_id, model, price_type, time_range),
        ).fetchone()
        if ps:
            print(f"  [EXISTS] {model} ({price_type}/{time_range})")
            continue
        print(f"  [INSERT] {model} input={input_price} output={output_price}")
        pending.append((
            f"price_snapshot insert: {model}",
            lambda cid=glm_channel_id, m=model, pt=price_type, tr=time_range,
                   ip=input_price, op=output_price: conn.execute(
                "INSERT INTO price_snapshots"
                " (channel_id, model, price_type, time_range,"
                "  input_price, output_price, unit, source)"
                " VALUES (?, ?, ?, ?, ?, ?, '元/百万tokens', 'manual')",
                (cid, m, pt, tr, ip, op),
            ),
        ))

    # ============ 5. 汇总 + 执行 ============
    if not pending:
        print("\n[OK] 所有数据已就绪, 无需操作")
        conn.close()
        return

    print(f"\n共 {len(pending)} 项操作待执行:")
    for desc, _ in pending:
        print(f"  - {desc}")

    if not args.apply:
        print("\n[DRY-RUN] 加 --apply 才执行")
        conn.close()
        return

    print("\n[APPLY] 开始执行...")
    try:
        conn.execute("BEGIN")
        for desc, fn in pending:
            fn()
            print(f"  [OK] {desc}")
        conn.execute("COMMIT")
        print("\n[OK] 完成; 等待路由缓存 TTL 刷新或 POST /dashboard/api/cache/refresh")
    except Exception as e:
        conn.execute("ROLLBACK")
        print(f"[FAIL] 执行失败: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
