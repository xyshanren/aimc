"""V4.9.4 migration: 修复 model_registry tier 腐坏 (2026-08-25)

背景:
- scripts/sync_siliconflow_to_registry.py 曾被手动执行 (--apply),
  其 line 210 `ex.tier = c["tier"]` 用 infer_tier() 结果覆写了 seed 脚本
  设置的正确 tier 值
- infer_tier() 基于参数规模关键词匹配, 对现代模型名 (deepseek-v4-flash,
  glm-5.2 等) 无法识别, 全部返回默认 T2
- 导致 tier:strong (要求 T1) 的成员全部被跳过 → fallback 到 tier:balanced
  → 实际服务 deepseek-v3 (旧模型) 而非 deepseek-v4-flash
- 同理 infer_modality() 对 minimax-m3 返回 "text" 而非 "multimodal"

修复:
- 按 seed_model_registry.py 的权威值恢复 tier 和 modality
- 修复后 tier:strong 路由恢复: deepseek-v4-flash (T1) 重新可选

sync 脚本已同步修复 (不再覆写已有模型的 tier/modality), 本迁移一次性
修复历史腐坏数据。

用法:
    python db/migrations/2026_08_25_fix_tier_corruption.py            # dry-run
    python db/migrations/2026_08_25_fix_tier_corruption.py --apply   # 实际修复
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


# 修复清单: (model_id, correct_tier, correct_modality, reason)
# tier/modality 权威来源: scripts/seed_model_registry.py (30 个 seed 模型)
# + scripts/seed_model_groups.py 注释 (glm-5.2 为 T1 旗舰)
FIXES = [
    # DeepSeek V4 系列 — seed 中 deepseek-v4-pro=T0, deepseek-v4-flash=T1
    ("deepseek-v4-pro", "T0", "text",
     "seed T0, 被 infer_tier 覆写为 T2"),
    ("deepseek-v4-flash", "T1", "text",
     "seed T1, 被 infer_tier 覆写为 T2"),

    # MiniMax — seed 中 m3=T1/multimodal, m2.7=T1, m2.7-highspeed=T2, m2.5=T2
    ("minimax-m3", "T1", "multimodal",
     "seed T1/multimodal, 被 infer_tier 覆写为 T3 (匹配 'mini'), infer_modality 覆写为 text"),
    ("minimax-m2.7", "T1", "text",
     "seed T1, 被 infer_tier 覆写为 T2"),
    ("minimax-m2.7-highspeed", "T2", "text",
     "seed T2, 被 infer_tier 覆写为 T3 (匹配 'highspeed' 无命中→T2, 需确认)"),
    ("minimax-m2.5", "T2", "text",
     "seed T2, 被 infer_tier 覆写为 T3 (匹配 'mini' in 'minimax')"),

    # GLM — glm-5.2 不在 seed_model_registry.py 中, 但 seed_model_groups.py
    # 注释明确标注为 "T1 旗舰", 且放在 tier:flagship 组 (该组不限 tier)
    ("glm-5.2", "T1", "text",
     "seed_model_groups 注释 T1 旗舰, 被 infer_tier 设为 T2"),
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    db_path = get_db_path()
    print(f"db path: {db_path}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # ============ 1. 打印当前状态 vs 目标 ============
    print("\n=== Tier 腐坏检查 ===")
    print(f"{'model_id':<28} {'当前 tier':<10} {'目标 tier':<10} "
          f"{'当前 modality':<14} {'目标 modality':<14} {'需修复':<6}")
    print("-" * 90)

    needs_fix = []
    for model_id, target_tier, target_modality, reason in FIXES:
        row = conn.execute(
            "SELECT tier, modality FROM model_registry WHERE model_id = ?",
            (model_id,),
        ).fetchone()

        if not row:
            print(f"  [SKIP] {model_id} 不在 model_registry 中")
            continue

        cur_tier = row["tier"]
        cur_modality = row["modality"]
        tier_bad = cur_tier != target_tier
        modality_bad = cur_modality != target_modality
        needs = tier_bad or modality_bad

        if needs:
            needs_fix.append((model_id, target_tier, target_modality, reason))

        flag = "YES" if needs else "no"
        print(f"  {model_id:<28} {cur_tier:<10} {target_tier:<10} "
              f"{cur_modality:<14} {target_modality:<14} {flag}")

    if not needs_fix:
        print("\n[OK] 所有模型 tier/modality 已正确, 无需修复")
        conn.close()
        return

    print(f"\n共 {len(needs_fix)} 个模型需要修复")
    for model_id, target_tier, target_modality, reason in needs_fix:
        print(f"  - {model_id}: → tier={target_tier}, modality={target_modality}")
        print(f"    原因: {reason}")

    if not args.apply:
        print("\n[DRY-RUN] 加 --apply 才执行修复")
        conn.close()
        return

    # ============ 2. 执行修复 ============
    print("\n[APPLY] 开始修复...")
    try:
        conn.execute("BEGIN")
        for model_id, target_tier, target_modality, reason in needs_fix:
            conn.execute(
                "UPDATE model_registry SET tier = ?, modality = ? "
                "WHERE model_id = ?",
                (target_tier, target_modality, model_id),
            )
            print(f"  [FIX] {model_id}: tier={target_tier}, modality={target_modality}")
        conn.execute("COMMIT")
        print(f"\n[OK] 修复 {len(needs_fix)} 个模型")
    except Exception as e:
        conn.execute("ROLLBACK")
        print(f"[FAIL] 修复失败: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
