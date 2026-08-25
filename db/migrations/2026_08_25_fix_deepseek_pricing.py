"""V4.9.5 migration: 修复 DeepSeek 官方价格 + 上线 deepseek-v4-flash-vision-exp (2026-08-25)

背景:
- DeepSeek 官方渠道 (deepseek_official, channel_id=1) 的 price_snapshots 有两类问题:
  1. time_range 错误: offpeak 用了 '00:00-08:00' (应为 '非高峰全天')
  2. 价格过时: v4-flash offpeak 写的 0.5/1.0, v4-pro normal 写的 4.0/16.0
  3. 缺少 cache 命中价 (cached_input_price)
  4. 缺少 deepseek-v4-flash-vision-exp (视觉实验版, 已上线一周)
- 官方价格来源: https://api-docs.deepseek.com/zh-cn/quick_start/pricing

修复内容:
1. 删除 deepseek_official 上所有旧 price_snapshots (id=114, 115)
2. 插入 9 行正确的 price_snapshots (3 model × 3 time_range):
   - deepseek-v4-pro: peak 9.0/27.0, offpeak 4.5/13.5
   - deepseek-v4-flash: peak 3.0/9.0, offpeak 1.5/4.5
   - deepseek-v4-flash-vision-exp: 同 v4-flash
3. 将 deepseek-v4-flash-vision-exp 加入 model_registry (T1, vision)
4. 将 deepseek-v4-flash-vision-exp 加入 tier:vision 组 (priority=105)

用法:
    python db/migrations/2026_08_25_fix_deepseek_pricing.py            # dry-run
    python db/migrations/2026_08_25_fix_deepseek_pricing.py --apply   # 实际修复
"""
from __future__ import annotations

import argparse
import json
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


# DeepSeek 官方渠道名
DEEPSEEK_CHANNEL_NAME = "deepseek_official"

# 正确的 price_snapshots: (model, price_type, time_range, input_price, output_price, cached_input_price)
# 来源: https://api-docs.deepseek.com/zh-cn/quick_start/pricing (2026-08-25 查询)
# 高峰: 工作日 09:00-12:00, 14:00-18:00; 空闲: 其余时段 + 周末全天
CORRECT_PRICES = [
    # deepseek-v4-pro: 高峰 9.0/27.0, 空闲 4.5/13.5, cache 0.30/0.15
    ("deepseek-v4-pro", "peak", "工作日09:00-12:00", 9.0, 27.0, 0.30),
    ("deepseek-v4-pro", "peak", "工作日14:00-18:00", 9.0, 27.0, 0.30),
    ("deepseek-v4-pro", "offpeak", "非高峰全天", 4.5, 13.5, 0.15),
    # deepseek-v4-flash: 高峰 3.0/9.0, 空闲 1.5/4.5, cache 0.10/0.05
    ("deepseek-v4-flash", "peak", "工作日09:00-12:00", 3.0, 9.0, 0.10),
    ("deepseek-v4-flash", "peak", "工作日14:00-18:00", 3.0, 9.0, 0.10),
    ("deepseek-v4-flash", "offpeak", "非高峰全天", 1.5, 4.5, 0.05),
    # deepseek-v4-flash-vision-exp: 同 v4-flash 价格, 图片转 token 后合并计费
    ("deepseek-v4-flash-vision-exp", "peak", "工作日09:00-12:00", 3.0, 9.0, 0.10),
    ("deepseek-v4-flash-vision-exp", "peak", "工作日14:00-18:00", 3.0, 9.0, 0.10),
    ("deepseek-v4-flash-vision-exp", "offpeak", "非高峰全天", 1.5, 4.5, 0.05),
]

# deepseek-v4-flash-vision-exp 的 model_registry 信息
VISION_MODEL_REGISTRY = {
    "model_id": "deepseek-v4-flash-vision-exp",
    "family": "deepseek",
    "display_name": "DeepSeek V4 Flash Vision Exp",
    "tier": "T1",
    "capabilities": json.dumps(["vision", "reasoning", "code", "general"], ensure_ascii=False),
    "context_length": 128000,
    "modality": "vision",
    "is_open_source": 0,
    "release_date": "2026-08",
    "status": "active",
    "notes": "V4 Flash 视觉实验版, 图片转 token 计费, 价格同 v4-flash",
}

# tier:vision 组中的 priority (在 minimax-m3=100 和 qwen-vl-32b-instruct=110 之间)
VISION_MODEL_GROUP_PRIORITY = 105


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    db_path = get_db_path()
    print(f"db path: {db_path}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # ============ 1. 查找 deepseek_official channel_id ============
    ch = conn.execute(
        "SELECT id, name FROM channels WHERE name = ?",
        (DEEPSEEK_CHANNEL_NAME,),
    ).fetchone()
    if not ch:
        print(f"[FAIL] 渠道 '{DEEPSEEK_CHANNEL_NAME}' 不存在")
        conn.close()
        return
    channel_id = ch["id"]
    print(f"渠道: {ch['name']} (id={channel_id})")

    # ============ 2. 打印当前 price_snapshots ============
    print("\n=== 当前 deepseek_official price_snapshots ===")
    current_prices = conn.execute(
        "SELECT id, model, price_type, time_range, input_price, output_price, "
        "cached_input_price FROM price_snapshots WHERE channel_id = ? "
        "ORDER BY model, price_type",
        (channel_id,),
    ).fetchall()
    for r in current_prices:
        print(f"  id={r['id']}: {r['model']:30s} {r['price_type']:8s} "
              f"'{r['time_range']:20s}' in={r['input_price']} out={r['output_price']} "
              f"cache={r['cached_input_price']}")
    print(f"  共 {len(current_prices)} 行")

    # ============ 3. 检查 deepseek-v4-flash-vision-exp 是否已在 model_registry ============
    vision_model = conn.execute(
        "SELECT model_id, tier, modality FROM model_registry "
        "WHERE model_id = ?",
        (VISION_MODEL_REGISTRY["model_id"],),
    ).fetchone()
    print(f"\n=== model_registry 中 {VISION_MODEL_REGISTRY['model_id']} ===")
    if vision_model:
        print(f"  已存在: tier={vision_model['tier']}, modality={vision_model['modality']}")
    else:
        print(f"  不存在 (将新增)")

    # ============ 4. 检查 tier:vision 组成员 ============
    vision_members = conn.execute(
        "SELECT model_id, priority FROM model_group_members "
        "WHERE group_id = 'tier:vision' AND is_active = 1 "
        "ORDER BY priority",
    ).fetchall()
    print(f"\n=== tier:vision 组当前成员 ({len(vision_members)} 个) ===")
    for m in vision_members:
        print(f"  {m['model_id']:30s} priority={m['priority']}")
    vision_already_member = any(
        m["model_id"] == VISION_MODEL_REGISTRY["model_id"] for m in vision_members
    )

    # ============ 5. 预览变更 ============
    print("\n=== 变更预览 ===")
    print(f"  1. 删除 deepseek_official 上 {len(current_prices)} 行旧 price_snapshots")
    print(f"  2. 插入 {len(CORRECT_PRICES)} 行正确 price_snapshots:")
    for model, ptype, tr, inp, outp, cache in CORRECT_PRICES:
        print(f"     {model:30s} {ptype:8s} '{tr:20s}' in={inp} out={outp} cache={cache}")
    if not vision_model:
        print(f"  3. 新增 model_registry: {VISION_MODEL_REGISTRY['model_id']} "
              f"(tier={VISION_MODEL_REGISTRY['tier']}, modality={VISION_MODEL_REGISTRY['modality']})")
    if not vision_already_member:
        print(f"  4. 新增 tier:vision 组成员: {VISION_MODEL_REGISTRY['model_id']} "
              f"(priority={VISION_MODEL_GROUP_PRIORITY})")

    if not args.apply:
        print("\n[DRY-RUN] 加 --apply 才执行修复")
        conn.close()
        return

    # ============ 6. 执行修复 ============
    print("\n[APPLY] 开始修复...")
    try:
        conn.execute("BEGIN")

        # 6a. 删除旧 price_snapshots
        conn.execute(
            "DELETE FROM price_snapshots WHERE channel_id = ?",
            (channel_id,),
        )
        print(f"  [DEL] 删除 {len(current_prices)} 行旧 price_snapshots")

        # 6b. 插入正确 price_snapshots
        for model, ptype, tr, inp, outp, cache in CORRECT_PRICES:
            conn.execute(
                "INSERT INTO price_snapshots "
                "(channel_id, model, price_type, time_range, input_price, output_price, "
                "cached_input_price, source, unit, currency, is_promotion) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'manual', '元/百万tokens', 'CNY', 0)",
                (channel_id, model, ptype, tr, inp, outp, cache),
            )
        print(f"  [INS] 插入 {len(CORRECT_PRICES)} 行正确 price_snapshots")

        # 6c. 新增 model_registry (如果不存在)
        if not vision_model:
            conn.execute(
                "INSERT INTO model_registry "
                "(model_id, family, display_name, tier, capabilities, context_length, "
                "modality, is_open_source, release_date, status, notes, supports_prompt_cache) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
                (
                    VISION_MODEL_REGISTRY["model_id"],
                    VISION_MODEL_REGISTRY["family"],
                    VISION_MODEL_REGISTRY["display_name"],
                    VISION_MODEL_REGISTRY["tier"],
                    VISION_MODEL_REGISTRY["capabilities"],
                    VISION_MODEL_REGISTRY["context_length"],
                    VISION_MODEL_REGISTRY["modality"],
                    VISION_MODEL_REGISTRY["is_open_source"],
                    VISION_MODEL_REGISTRY["release_date"],
                    VISION_MODEL_REGISTRY["status"],
                    VISION_MODEL_REGISTRY["notes"],
                ),
            )
            print(f"  [INS] model_registry 新增: {VISION_MODEL_REGISTRY['model_id']}")

        # 6d. 新增 tier:vision 组成员 (如果不存在)
        if not vision_already_member:
            conn.execute(
                "INSERT INTO model_group_members "
                "(group_id, model_id, priority, weight, added_by, is_active) "
                "VALUES (?, ?, ?, 1.0, 'manual', 1)",
                ("tier:vision", VISION_MODEL_REGISTRY["model_id"],
                 VISION_MODEL_GROUP_PRIORITY),
            )
            print(f"  [INS] tier:vision 组新增: {VISION_MODEL_REGISTRY['model_id']} "
                  f"(priority={VISION_MODEL_GROUP_PRIORITY})")

        conn.execute("COMMIT")
        print("\n[OK] 修复完成")

    except Exception as e:
        conn.execute("ROLLBACK")
        print(f"[FAIL] 修复失败: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
