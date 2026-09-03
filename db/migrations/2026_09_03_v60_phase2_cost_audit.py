"""V6.0 Phase 2 migration: 标价双轨成本审计 (2026-09-03)

背景:
- 生产近 7 天 95%+ 请求实付 cost_estimate=0 (sensenova TokenPlan / glm coding plan
  订阅 / GLM 免费模型), 看板只剩"花了 0 元", 无法回答"省了多少"
- V6.0 Phase 2 (docs/next-major-v6-plan.md §2.3 Phase A): 标价双轨 —
  model_registry 增加官方标价列, usage_logs 增加标价估算列,
  节省金额 = 标价 - 实付 (仅对标价已知的行聚合)

动作 (全部幂等, 可重复执行):
1. model_registry ADD COLUMN list_input_price / list_output_price (已存在则跳过)
2. usage_logs ADD COLUMN list_cost_estimate (已存在则跳过)
3. 回填官方标价 (仅写 list_input_price IS NULL 的行 — 不覆盖后续人工修正)
4. 回填存量 usage_logs.list_cost_estimate (仅 NULL 行, 按 model JOIN registry 标价;
   标价未知的模型历史行保持 NULL — 与新请求口径一致)

标价来源与口径 (不编造, 未知的留 NULL):
- GLM 官方价格表 (2026-09, open.bigmodel.cn/pricing, 用户抓取确认, 见计划文档 §1.1)
  - glm-5.3-flash 用原价标价 0.8/2.8 (当前限时 5 折 0.4/1.4, 优惠期后恢复原价)
  - glm-4.7-flash / glm-4.6v-flash 官方明确免费 → 标 0.0 (与 NULL=未知 区分)
- deepseek_official 渠道峰时标准价 (offpeak 1.5/4.5 是时段折扣, 标价取标准价)
- 硅基流动付费行 (sibling 市场价, e.g. zai-org/GLM-5.2 8.0/28.0)
- minimax-m3 / minimax-m2.7 / kimi-k3 / sensenova-* / deepseek-v3: 官方标价未知 → NULL,
  由成本审计 API 的覆盖率指标暴露, 后续补数据时再回填

用法:
    python db/migrations/2026_09_03_v60_phase2_cost_audit.py            # dry-run
    python db/migrations/2026_09_03_v60_phase2_cost_audit.py --apply   # 实际执行
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


# (model_id, list_input_price, list_output_price) — 元/百万tokens, CNY
LIST_PRICES: list[tuple[str, float, float]] = [
    # GLM 官方价格表 (2026-09)
    ("glm-5.3", 8.0, 28.0),
    ("glm-5.3-flash", 0.8, 2.8),      # 原价标价; 限时 5 折 0.4/1.4 期间实付更低
    ("glm-5.2", 8.0, 28.0),
    ("glm-4.7-flash", 0.0, 0.0),      # 官方免费 (节省=0 是真实结论, 区别于 NULL=未知)
    ("glm-4.6v-flash", 0.0, 0.0),     # 官方免费
    ("glm-4-flash", 0.1, 0.1),
    ("glm-4.6v", 1.0, 3.0),
    ("glm-4.5v", 1.5, 4.0),           # 硅基 zai-org/GLM-4.5V 付费行
    # DeepSeek 官方标准价 (= deepseek_official 峰时价; offpeak 属时段折扣)
    ("deepseek-v4-flash", 3.0, 9.0),
    ("deepseek-v4-pro", 9.0, 27.0),
    ("deepseek-v4-flash-vision-exp", 3.0, 9.0),
    # 硅基流动付费行 (sibling 市场价)
    ("deepseek-v3.2", 2.0, 3.0),
    ("qwen-vl-32b-instruct", 1.0, 4.0),
    ("qwen-vl-32b-thinking", 1.0, 10.0),
    ("qwen-vl-30b-a3b-thinking", 0.7, 2.8),
    ("qwen-vl-8b-instruct", 0.5, 2.0),
    ("qwen-qwen3-8b", 2.0, 3.0),
    ("qwen-qwen3.5-4b", 1.6, 12.8),
    ("minimax-m2.5", 2.0, 1.2),
]


def run(apply: bool) -> int:
    db_path = get_db_path()
    print(f"db path: {db_path}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # PRAGMA 不支持参数绑定, 表名是代码内常量, 直接内联字面量
    registry_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(model_registry)")
    }
    usage_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(usage_logs)")
    }

    # pending: (描述, 执行函数) — Mimosa 约束: conn.execute 第一个参数必须内联字面量
    pending: list[tuple[str, object]] = []

    if "list_input_price" not in registry_cols:
        pending.append((
            "model_registry ADD COLUMN list_input_price",
            lambda: conn.execute(
                "ALTER TABLE model_registry ADD COLUMN list_input_price FLOAT"
            ),
        ))
    if "list_output_price" not in registry_cols:
        pending.append((
            "model_registry ADD COLUMN list_output_price",
            lambda: conn.execute(
                "ALTER TABLE model_registry ADD COLUMN list_output_price FLOAT"
            ),
        ))
    if "list_cost_estimate" not in usage_cols:
        pending.append((
            "usage_logs ADD COLUMN list_cost_estimate",
            lambda: conn.execute(
                "ALTER TABLE usage_logs ADD COLUMN list_cost_estimate FLOAT"
            ),
        ))

    # 标价回填计划 (仅 NULL 行; 模型不在 registry 则跳过)
    registry_ids = {
        r[0] for r in conn.execute("SELECT model_id FROM model_registry")
    }
    already = {
        r[0]
        for r in conn.execute(
            "SELECT model_id FROM model_registry WHERE list_input_price IS NOT NULL"
        )
    } if "list_input_price" in registry_cols else set()

    backfill_plan: list[tuple[str, float, float]] = []
    skipped_missing: list[str] = []
    for model_id, in_p, out_p in LIST_PRICES:
        if model_id not in registry_ids:
            skipped_missing.append(model_id)
            continue
        if model_id in already:
            continue  # 已有标价 (前次执行或人工修正), 不覆盖
        backfill_plan.append((model_id, in_p, out_p))

    for model_id, in_p, out_p in backfill_plan:
        pending.append((
            f"backfill 标价 {model_id}: {in_p}/{out_p}",
            lambda m=model_id, i=in_p, o=out_p: conn.execute(
                "UPDATE model_registry "
                "SET list_input_price = ?, list_output_price = ? "
                "WHERE model_id = ? AND list_input_price IS NULL",
                (i, o, m),
            ),
        ))

    # 步骤 4: 存量 usage_logs 标价回填 (仅 NULL 行; 标价未知模型的历史行保持 NULL)
    # 前提: usage_logs 具备 model/input_tokens/output_tokens 列 (极旧库缺列则跳过)
    if (
        "list_cost_estimate" in usage_cols and "list_input_price" in registry_cols
        and all(c in usage_cols for c in ("model", "input_tokens", "output_tokens"))
    ):
        n_usage = conn.execute(
            "SELECT COUNT(*) FROM usage_logs "
            "WHERE list_cost_estimate IS NULL AND model IN ("
            "  SELECT model_id FROM model_registry "
            "  WHERE list_input_price IS NOT NULL AND list_output_price IS NOT NULL)"
        ).fetchone()[0]
        if n_usage:
            pending.append((
                f"回填存量 usage_logs 标价 ({n_usage} 行)",
                lambda: conn.execute(
                    "UPDATE usage_logs SET list_cost_estimate = ("
                    "  SELECT (COALESCE(usage_logs.input_tokens, 0) * r.list_input_price"
                    "        + COALESCE(usage_logs.output_tokens, 0) * r.list_output_price"
                    "  ) / 1000000.0 "
                    "  FROM model_registry r "
                    "  WHERE r.model_id = usage_logs.model "
                    "  AND r.list_input_price IS NOT NULL "
                    "  AND r.list_output_price IS NOT NULL"
                    ") WHERE usage_logs.list_cost_estimate IS NULL"
                ),
            ))

    print(f"\n待执行动作 ({len(pending)} 项):")
    for desc, _ in pending:
        print(f"  - {desc}")
    if skipped_missing:
        print(f"\n跳过 (不在 registry): {', '.join(skipped_missing)}")
    known_after = len({m for m, _, _ in LIST_PRICES} & registry_ids)
    unknown = sorted(registry_ids - {m for m, _, _ in LIST_PRICES} - already)
    print(f"\n标价覆盖: {known_after}/{len(registry_ids)} 个 registry 模型")
    if unknown:
        print(f"标价未知 (留 NULL): {', '.join(unknown)}")

    if not pending:
        print("\n[OK] 无需任何变更")
        conn.close()
        return 0

    if not apply:
        print("\n[DRY-RUN] 加 --apply 才执行")
        conn.close()
        return 0

    print("\n[APPLY] 开始执行...")
    try:
        conn.execute("BEGIN")
        for desc, fn in pending:
            fn()
            print(f"  [OK] {desc}")
        conn.execute("COMMIT")
    except Exception as e:
        conn.execute("ROLLBACK")
        print(f"[FAIL] 执行失败, 已回滚: {e}")
        conn.close()
        raise

    # 终态确认
    n = conn.execute(
        "SELECT COUNT(*) FROM model_registry WHERE list_input_price IS NOT NULL"
    ).fetchone()[0]
    print(f"\n[OK] 完成: {n} 个模型有官方标价")
    conn.close()
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    sys.exit(run(apply=args.apply))


if __name__ == "__main__":
    main()
