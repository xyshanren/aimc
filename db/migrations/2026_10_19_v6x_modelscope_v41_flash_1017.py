"""V6.x migration: modelscope DeepSeek raw id 轮换到上游现役 id (2026-10-19)

背景:
- DeepSeek V4.1-Flash-1017 版本上线 (2026-10-17), ModelScope 同步上架新 raw id
  `deepseek-ai/DeepSeek-V4.1-Flash-1017`; 旧 raw id `deepseek-ai/DeepSeek-V4.1-Flash`
  上游已退役 —— 生产直连实证 (2026-10-19, modelscope_01 账号):
    400 "Model id : deepseek-ai/DeepSeek-V4.1-Flash , has no provider supported"
- 同批实证: `deepseek-ai/DeepSeek-V4-Pro` 也已退役, 现役 id =
  `deepseek-ai/DeepSeek-V4-Pro-0813` (400 "no provider supported")
- 后果: 400 = client_error 不可重试, 不会 failover 到 ch1 官方付费兜底 →
  ① tier:flagship 首成员 deepseek-flash 首选 modelscope 0 元供给时整组请求失败
  ② deepseek-v4-pro 精确直连整条链失败 (即便 ch1 有付费价行也轮不到)
- modelscope /v1/models 仍列旧 id (上架列表滞后于推理侧退役), 以推理 400 为准

改动 (幂等收敛, 仅 modelscope_inference 渠道, 两个模型统一处理):
1. 规范行 (model=deepseek-flash / deepseek-v4-pro) 的 raw_model_id → 现役 id
2. raw 行 (model=旧 raw id) 的 model + raw_model_id → 现役 id (直连入口)
3. channels_catalog.py RAW_ALIASES / prices 同步 (代码侧, 非本迁移)

用法:
    python db/migrations/2026_10_19_v6x_modelscope_v41_flash_1017.py            # dry-run
    python db/migrations/2026_10_19_v6x_modelscope_v41_flash_1017.py --apply   # 实际执行
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

# (旧 raw id, 现役 raw id, 规范名)
ROTATIONS = (
    ("deepseek-ai/DeepSeek-V4.1-Flash", "deepseek-ai/DeepSeek-V4.1-Flash-1017", "deepseek-flash"),
    ("deepseek-ai/DeepSeek-V4-Pro", "deepseek-ai/DeepSeek-V4-Pro-0813", "deepseek-v4-pro"),
)
CHANNEL_NAME = "modelscope_inference"


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

    ch_row = conn.execute(
        "SELECT id, name FROM channels WHERE name = ?", (CHANNEL_NAME,)
    ).fetchone()
    if not ch_row:
        print(f"[FAIL] {CHANNEL_NAME} 渠道不存在, 中止")
        conn.close()
        return
    channel_id = ch_row["id"]
    print(f"channel: id={channel_id} name={ch_row['name']}")

    print("\n=== 1. 目标行检查 ===")
    # 待执行: [(动作, 行 id, 旧值, 新值)]
    actions = []
    for old_raw, new_raw, canonical in ROTATIONS:
        canon = conn.execute(
            "SELECT id, model, raw_model_id FROM price_snapshots "
            "WHERE channel_id = ? AND model = ?",
            (channel_id, canonical),
        ).fetchone()
        if canon:
            if canon["raw_model_id"] == new_raw:
                print(f"  [OK] 规范行 id={canon['id']} ({canonical}) raw 已指向 {new_raw}")
            elif canon["raw_model_id"] == old_raw:
                actions.append(("canon", canon["id"], old_raw, new_raw))
                print(f"  [TO-UPDATE] 规范行 id={canon['id']} ({canonical}) raw: "
                      f"{old_raw} → {new_raw}")
            else:
                print(f"  [SKIP] 规范行 id={canon['id']} ({canonical}) raw="
                      f"{canon['raw_model_id']} 非预期, 不动")
        else:
            print(f"  [MISS] 无规范行 model={canonical}")

        raw_row = conn.execute(
            "SELECT id, model, raw_model_id FROM price_snapshots "
            "WHERE channel_id = ? AND model = ?",
            (channel_id, old_raw),
        ).fetchone()
        if raw_row:
            actions.append(("raw", raw_row["id"], old_raw, new_raw))
            print(f"  [TO-UPDATE] raw 行 id={raw_row['id']} model: {old_raw} → {new_raw}")
        else:
            print(f"  [NONE] 无旧 raw 行 model={old_raw}")

    if not actions:
        print("\n[OK] 数据已就绪, 无需操作")
        conn.close()
        return

    if not args.apply:
        print("\n[DRY-RUN] 加 --apply 才执行")
        conn.close()
        return

    print("\n[APPLY] 开始迁移...")
    try:
        conn.execute("BEGIN")
        for kind, row_id, old_raw, new_raw in actions:
            if kind == "canon":
                conn.execute(
                    "UPDATE price_snapshots SET raw_model_id = ? WHERE id = ?",
                    (new_raw, row_id),
                )
                print(f"  [OK] 规范行 id={row_id} raw_model_id → {new_raw}")
            else:
                conn.execute(
                    "UPDATE price_snapshots SET model = ?, raw_model_id = ? WHERE id = ?",
                    (new_raw, new_raw, row_id),
                )
                print(f"  [OK] raw 行 id={row_id} → {new_raw}")
        conn.execute("COMMIT")
        print("\n[OK] 迁移完成")
    except Exception as e:
        conn.execute("ROLLBACK")
        print(f"[FAIL] 迁移失败: {e}")
        raise

    print("\n=== 迁移后 modelscope 价行 ===")
    for r in conn.execute(
        "SELECT ps.id, ps.model, ps.raw_model_id FROM price_snapshots ps "
        "WHERE ps.channel_id = ? ORDER BY ps.id",
        (channel_id,),
    ):
        print(f"  id={r['id']:4d} model={r['model']:42s} raw={r['raw_model_id']}")

    conn.close()


if __name__ == "__main__":
    main()
