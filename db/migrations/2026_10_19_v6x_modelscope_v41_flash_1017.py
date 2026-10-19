"""V6.x migration: modelscope DeepSeek raw id 轮换到 V4.1-Flash-1017 (2026-10-19)

背景:
- DeepSeek V4.1-Flash-1017 版本上线 (2026-10-17), ModelScope 同步上架新 raw id
  `deepseek-ai/DeepSeek-V4.1-Flash-1017`; 旧 raw id `deepseek-ai/DeepSeek-V4.1-Flash`
  上游已退役 —— 生产直连实证 (2026-10-19, modelscope_01 账号):
    400 "Model id : deepseek-ai/DeepSeek-V4.1-Flash , has no provider supported"
- 后果: tier:flagship 首成员 deepseek-flash 首选 modelscope 0 元供给时整组请求
  400 (400 = client_error 不可重试, 不会 failover 到 ch1 官方付费兜底) →
  flagship 组实际无可用候选
- modelscope /v1/models 仍列旧 id (上架列表滞后于推理侧退役), 以推理 400 为准

改动 (幂等收敛, 仅 modelscope_inference 渠道):
1. 规范行 (model=deepseek-flash) 的 raw_model_id → 新 id
2. raw 行 (model=旧 raw id) 的 model → 新 id (直连入口)
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

OLD_RAW = "deepseek-ai/DeepSeek-V4.1-Flash"
NEW_RAW = "deepseek-ai/DeepSeek-V4.1-Flash-1017"
CANONICAL = "deepseek-flash"


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
        "SELECT id, name FROM channels WHERE name = 'modelscope_inference'"
    ).fetchone()
    if not ch_row:
        print("[FAIL] modelscope_inference 渠道不存在, 中止")
        conn.close()
        return
    channel_id = ch_row["id"]
    print(f"channel: id={channel_id} name={ch_row['name']}")

    print("\n=== 1. 目标行检查 ===")
    canon = conn.execute(
        "SELECT id, model, raw_model_id FROM price_snapshots "
        "WHERE channel_id = ? AND model = ?",
        (channel_id, CANONICAL),
    ).fetchone()
    raw_row = conn.execute(
        "SELECT id, model, raw_model_id FROM price_snapshots "
        "WHERE channel_id = ? AND model = ?",
        (channel_id, OLD_RAW),
    ).fetchone()
    already = conn.execute(
        "SELECT id, model, raw_model_id FROM price_snapshots "
        "WHERE channel_id = ? AND (model = ? OR raw_model_id = ?)",
        (channel_id, NEW_RAW, NEW_RAW),
    ).fetchall()

    canon_action = False
    if canon:
        if canon["raw_model_id"] == NEW_RAW:
            print(f"  [OK] 规范行 id={canon['id']} raw 已指向新 id")
        else:
            canon_action = True
            print(f"  [TO-UPDATE] 规范行 id={canon['id']} raw: "
                  f"{canon['raw_model_id']} → {NEW_RAW}")
    else:
        print(f"  [MISS] 无规范行 model={CANONICAL}")

    raw_action = False
    if raw_row:
        raw_action = True
        print(f"  [TO-UPDATE] raw 行 id={raw_row['id']} model: "
              f"{raw_row['model']} → {NEW_RAW}")
    else:
        print(f"  [NONE] 无旧 raw 行 model={OLD_RAW}")

    for r in already:
        print(f"  [INFO] 已指向新 id 的行: id={r['id']} model={r['model']} "
              f"raw={r['raw_model_id']}")

    if not canon_action and not raw_action:
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
        if canon_action:
            conn.execute(
                "UPDATE price_snapshots SET raw_model_id = ? WHERE id = ?",
                (NEW_RAW, canon["id"]),
            )
            print(f"  [OK] 规范行 id={canon['id']} raw_model_id → {NEW_RAW}")
        if raw_action:
            conn.execute(
                "UPDATE price_snapshots SET model = ?, raw_model_id = ? WHERE id = ?",
                (NEW_RAW, NEW_RAW, raw_row["id"]),
            )
            print(f"  [OK] raw 行 id={raw_row['id']} → {NEW_RAW}")
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
