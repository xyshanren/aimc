"""V6.x migration: 修正 VL 系多模态检索模型 modality 误标 (2026-09-30)

背景:
- scripts/sync_siliconflow_to_registry.py 的 infer_modality() 中 vision
  关键词检查 (-vl-) 排在 embedding/reranker 检查之前, 导致
  *-VL-Embedding-* / *-VL-Reranker-* 这类多模态检索模型被误归 vision
- 生产库受影响行 (生产实测 2026-09-30):
    qwen3-vl-embedding-8b  vision → embedding
    qwen3-vl-reranker-8b   vision → embedding
- 误标本身不影响 embeddings/rerank 直通端点 (不读 modality), 但存在
  潜在风险: modality=vision 的模型若被注册进 tier:vision 组会被当
  chat 模型路由 (上游 400); dashboard 按 modality 展示也会误导

修复:
- infer_modality() 已同步修正 (embedding/reranker 检查前置, 只影响
  registry 中不存在的新模型)
- 本迁移一次性修正生产存量两行

用法:
    python db/migrations/2026_09_30_v6x_vl_embedding_modality_fix.py            # dry-run
    python db/migrations/2026_09_30_v6x_vl_embedding_modality_fix.py --apply   # 实际修复
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


# 修复清单: (model_id, target_modality, reason)
FIXES = [
    ("qwen3-vl-embedding-8b", "embedding",
     "多模态 embedding (图片向量化), 被 -vl- 关键词误归 vision"),
    ("qwen3-vl-reranker-8b", "embedding",
     "多模态 reranker, 同上 (与 vl-embedding 同族检索模型)"),
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
    print("\n=== VL 检索模型 modality 误标检查 ===")
    print(f"{'model_id':<28} {'当前 modality':<14} {'目标 modality':<14} {'需修复':<6}")
    print("-" * 70)

    needs_fix = []
    for model_id, target_modality, reason in FIXES:
        row = conn.execute(
            "SELECT modality FROM model_registry WHERE model_id = ?",
            (model_id,),
        ).fetchone()

        if not row:
            print(f"  [SKIP] {model_id} 不在 model_registry 中")
            continue

        cur = row["modality"]
        needs = cur != target_modality
        if needs:
            needs_fix.append((model_id, target_modality, reason))

        flag = "YES" if needs else "no"
        print(f"  {model_id:<28} {cur:<14} {target_modality:<14} {flag}")

    if not needs_fix:
        print("\n[OK] 所有模型 modality 已正确, 无需修复")
        conn.close()
        return

    print(f"\n共 {len(needs_fix)} 个模型需要修复")
    for model_id, target_modality, reason in needs_fix:
        print(f"  - {model_id}: modality → {target_modality}")
        print(f"    原因: {reason}")

    if not args.apply:
        print("\n[DRY-RUN] 加 --apply 才执行修复")
        conn.close()
        return

    # ============ 2. 执行修复 ============
    print("\n[APPLY] 开始修复...")
    try:
        conn.execute("BEGIN")
        for model_id, target_modality, reason in needs_fix:
            conn.execute(
                "UPDATE model_registry SET modality = ? "
                "WHERE model_id = ?",
                (target_modality, model_id),
            )
            print(f"  [FIX] {model_id}: modality={target_modality}")
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
