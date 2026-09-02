"""V5.1 migration: minimax-m3 tier T1 → T2 (2026-09-02)

背景:
- 用户能力评估 (人工确认, 符合 architecture.md §十 "tier 调整永远人工确认"):
  minimax-m3 能力低于 glm-5.3-flash (T2) 和 deepseek-v4-flash (T1),
  TokenPlanMax 年度会员额度充足但定位应为容量兜底而非主力
- V5.0 调整: m3 从 tier:strong 移到 tier:balanced 做兜底
- tier:balanced 限定 T2 (精确匹配), m3 保持 T1 会被组过滤判成死成员
  (与 2026_08_14 m2.7/v4-flash 在 tier:code 同类问题)
- 因此 registry tier 降档 T2, 同时保留 balanced 组的 T2 过滤约束

修复:
- model_registry.minimax-m3: tier T1 → T2 (modality=text 不变)

用法:
    python db/migrations/2026_09_02_v51_m3_tier_t2.py            # dry-run
    python db/migrations/2026_09_02_v51_m3_tier_t2.py --apply   # 实际修复
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


TARGET_MODEL = "minimax-m3"
TARGET_TIER = "T2"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    db_path = get_db_path()
    print(f"db path: {db_path}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    row = conn.execute(
        "SELECT tier, modality, status FROM model_registry WHERE model_id = ?",
        (TARGET_MODEL,),
    ).fetchone()

    if not row:
        print(f"[SKIP] {TARGET_MODEL} 不在 model_registry 中")
        conn.close()
        return

    print(f"\n{TARGET_MODEL}: 当前 tier={row['tier']}, "
          f"modality={row['modality']}, 目标 tier={TARGET_TIER}")

    if row["tier"] == TARGET_TIER:
        print("[OK] tier 已是 T2, 无需修复")
        conn.close()
        return

    if not args.apply:
        print("\n[DRY-RUN] 加 --apply 才执行修复")
        conn.close()
        return

    print("[APPLY] 开始修复...")
    try:
        conn.execute("BEGIN")
        conn.execute(
            "UPDATE model_registry SET tier = ? WHERE model_id = ?",
            (TARGET_TIER, TARGET_MODEL),
        )
        conn.execute("COMMIT")
        print(f"[OK] {TARGET_MODEL}: tier={TARGET_TIER} (modality 不变)")
    except Exception as e:
        conn.execute("ROLLBACK")
        print(f"[FAIL] 修复失败: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
