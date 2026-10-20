"""V6.x migration: modelscope 下线 2 条已退役 Qwen 行 + 新增 GLM-5.3-Flash 供给 (2026-10-19)

背景 (全部为生产直探实证, 上游 /v1/models 上架列表不作数):
- 已退役 (400 "Model id : ... , has no provider supported"):
    Qwen/Qwen3-Next-80B-A3B-Instruct (2026-09-14 用户实测时可用, 之后上游撤供)
    Qwen/Qwen3-235B-A22B (同上)
  → 两条都是"纯直连"行 (raw_model_id 为空, 无规范名行), 不参与组路由, 仅影响
    直连入口; 留着只会让客户端按 raw id 请求时吃 400, 移除后直连入口失效是
    诚实状态 (usage_logs 历史保留, 不受影响)
- 新增可用 (直探 200): ZhipuAI/GLM-5.3-Flash
  → 我们的 glm-5.3-flash 是 T1 multimodal (strong 120 / code 130), 现供给只有
    glm_bigmodel 单一 Lite 池 (积分系数 + 周池限制); 补 modelscope 0 元供给 =
    第二条免费车道, 降低对 Lite 池的依赖 (2026-10-19 直探 200)
  → 注: ZhipuAI/GLM-5.3 本身 400 no provider — ModelScope 没有 GLM-5.3, 别混
- 上游列表里另见 200 可用但**未注册表分级**的模型 (Qwen3.5-397B-A17B /
  Qwen3.5-122B-A10B / Qwen3.8-27B / Intern-S2-Preview / Nex-N2.5-Pro) —
  需先策展分级才能入组, 本迁移不涉及

改动 (幂等, 仅 modelscope_inference 渠道):
1. DELETE 2 条已退役 Qwen raw 价行
2. INSERT 规范行 model=glm-5.3-flash raw_model_id=ZhipuAI/GLM-5.3-Flash (0/0)
3. INSERT raw 行 model=ZhipuAI/GLM-5.3-Flash raw_model_id=同名 (直连入口)

用法:
    python db/migrations/2026_10_19_v6x_modelscope_glm53flash.py            # dry-run
    python db/migrations/2026_10_19_v6x_modelscope_glm53flash.py --apply   # 实际执行
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

DEAD_ROWS = (
    "Qwen/Qwen3-Next-80B-A3B-Instruct",
    "Qwen/Qwen3-235B-A22B",
)
NEW_RAW = "ZhipuAI/GLM-5.3-Flash"
NEW_CANON = "glm-5.3-flash"
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

    print("\n=== 1. 已退役 Qwen 行下线检查 ===")
    dead_rows = []
    for dead in DEAD_ROWS:
        r = conn.execute(
            "SELECT id, model, raw_model_id FROM price_snapshots "
            "WHERE channel_id = ? AND model = ?",
            (channel_id, dead),
        ).fetchone()
        if r:
            dead_rows.append(r["id"])
            print(f"  [TO-DELETE] id={r['id']} {r['model']} raw={r['raw_model_id']}")
        else:
            print(f"  [NONE] 无行 model={dead}")

    print("\n=== 2. GLM-5.3-Flash 供给行检查 ===")
    canon = conn.execute(
        "SELECT id FROM price_snapshots WHERE channel_id = ? AND model = ?",
        (channel_id, NEW_CANON),
    ).fetchone()
    raw_row = conn.execute(
        "SELECT id FROM price_snapshots WHERE channel_id = ? AND model = ?",
        (channel_id, NEW_RAW),
    ).fetchone()
    need_canon = canon is None
    need_raw = raw_row is None
    if canon:
        print(f"  [OK] 规范行已存在 id={canon['id']}")
    else:
        print(f"  [TO-INSERT] 规范行 model={NEW_CANON} raw_model_id={NEW_RAW} (0.0/0.0)")
    if raw_row:
        print(f"  [OK] raw 行已存在 id={raw_row['id']}")
    else:
        print(f"  [TO-INSERT] raw 行 model={NEW_RAW} (直连入口, 0.0/0.0)")

    if not dead_rows and not need_canon and not need_raw:
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
        for rid in dead_rows:
            conn.execute("DELETE FROM price_snapshots WHERE id = ?", (rid,))
            print(f"  [OK] 删除已退役行 id={rid}")
        if need_canon:
            conn.execute(
                "INSERT INTO price_snapshots "
                "(channel_id, model, price_type, time_range, input_price, output_price, "
                "unit, source, raw_model_id, currency) "
                "VALUES (?, ?, 'normal', '全天', 0.0, 0.0, '元/百万tokens', 'manual', ?, 'CNY')",
                (channel_id, NEW_CANON, NEW_RAW),
            )
            print(f"  [OK] 规范行入库: {NEW_CANON} -> {NEW_RAW}")
        if need_raw:
            conn.execute(
                "INSERT INTO price_snapshots "
                "(channel_id, model, price_type, time_range, input_price, output_price, "
                "unit, source, raw_model_id, currency) "
                "VALUES (?, ?, 'normal', '全天', 0.0, 0.0, '元/百万tokens', 'manual', ?, 'CNY')",
                (channel_id, NEW_RAW, NEW_RAW),
            )
            print(f"  [OK] raw 行入库: {NEW_RAW} (直连入口)")
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
