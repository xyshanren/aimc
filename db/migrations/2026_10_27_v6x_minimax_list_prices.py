"""V6.x migration: minimax-m3 官方按量标价入库 (registry list 双轨, 2026-10-27)

背景:
- MiniMax 官方定价页更新 (platform.minimax.cn/docs/pricing/overview, 用户 2026-10-27 提供):
    MiniMax-M3.1-Flash: 1.00 / 4.00, cache 读 0.10 元每百万tokens
    MiniMax-M3 标准 ≤512k 输入 (永久五折): 2.10 / 8.40, cache 0.42
    MiniMax-M3 标准 >512k 输入 (永久五折): 4.20 / 16.80, cache 0.84
    优先档 (service_tier=priority) = 标准价 1.5 倍 (AIMC 不启用)
- registry list_input_price/list_output_price = 官方按量标价 (成本审计
  「标价/节省」双轨, proxy 每次请求据此写 usage_logs.list_cost_estimate);
  price_snapshots 保持 0.0 (M Plan 订阅边际成本, 不变 — 用户拍板)
- 现状: minimax-m3.1-flash 已是 1.0/4.0 (2026-10-19 设, 与官方一致, 幂等确保);
  minimax-m3 为 NULL/NULL — 本迁移补 2.10/8.40
- 取档依据: 近 7 天生产 minimax-m3 用量 538 次、0 次 >512k 输入 → ≤512k 档
  覆盖面 100%; >512k 翻倍档 schema 无分档列, 记 notes 留档
- cache 读价 registry 无列, 记 notes (m3.1-flash 的 0.1 同)

改动 (幂等):
1. minimax-m3: list_input_price=2.10, list_output_price=8.40, notes 补定价依据
2. minimax-m3.1-flash: 确保 1.0/4.0 (已是则不动)

用法:
    python db/migrations/2026_10_27_v6x_minimax_list_prices.py            # dry-run
    python db/migrations/2026_10_27_v6x_minimax_list_prices.py --apply   # 实际执行
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

M3_TARGETS = {
    "minimax-m3": (2.10, 8.40),
    "minimax-m3.1-flash": (1.00, 4.00),
}
M3_NOTES = (
    "M3 标准档按量标价 (2026-10-27 官方定价页): ≤512k 输入 2.10/8.40 "
    "cache 读 0.42 元每百万tokens (永久五折后实付); >512k 输入翻倍 "
    "4.20/16.80 cache 0.84; 优先档=标准×1.5 (AIMC 不启用)。"
    "订阅渠道实际计费 0 元 (M Plan), 本标价仅成本审计双轨用。"
)


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

    updates = []
    for model_id, (inp, out) in M3_TARGETS.items():
        row = conn.execute(
            "SELECT model_id, list_input_price, list_output_price, notes "
            "FROM model_registry WHERE model_id = ?",
            (model_id,),
        ).fetchone()
        if not row:
            print(f"  [MISS] {model_id} 无注册表行, 跳过 (fresh seed 后再跑本迁移)")
            continue
        if row["list_input_price"] == inp and row["list_output_price"] == out:
            print(f"  [OK] {model_id} 已是 {inp}/{out}")
            continue
        updates.append((model_id, inp, out, row["notes"]))
        print(f"  [TO-UPDATE] {model_id}: "
              f"{row['list_input_price']}/{row['list_output_price']} → {inp}/{out}")

    if not updates:
        print("\n[OK] 所有数据已就绪, 无需操作")
        conn.close()
        return

    if not args.apply:
        print("\n[DRY-RUN] 加 --apply 才执行")
        conn.close()
        return

    print("\n[APPLY] 开始迁移...")
    try:
        conn.execute("BEGIN")
        for model_id, inp, out, old_notes in updates:
            if model_id == "minimax-m3":
                conn.execute(
                    "UPDATE model_registry SET list_input_price = ?, "
                    "list_output_price = ?, notes = ? WHERE model_id = ?",
                    (inp, out, M3_NOTES, model_id),
                )
                print(f"  [OK] {model_id} → {inp}/{out} + notes 定价依据")
            else:
                conn.execute(
                    "UPDATE model_registry SET list_input_price = ?, "
                    "list_output_price = ? WHERE model_id = ?",
                    (inp, out, model_id),
                )
                print(f"  [OK] {model_id} → {inp}/{out}")
        conn.execute("COMMIT")
        print("\n[OK] 迁移完成")
    except Exception as e:
        conn.execute("ROLLBACK")
        print(f"[FAIL] 迁移失败: {e}")
        raise

    print("\n=== 迁移后 minimax 系标价 ===")
    for r in conn.execute(
        "SELECT model_id, list_input_price, list_output_price FROM model_registry "
        "WHERE model_id LIKE 'minimax%' ORDER BY model_id"
    ):
        print(f"  {r['model_id']:24s} list={r['list_input_price']}/{r['list_output_price']}")

    conn.close()


if __name__ == "__main__":
    main()
