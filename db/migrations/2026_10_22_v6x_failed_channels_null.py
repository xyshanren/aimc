"""V6.18 migration: usage_logs.failed_channels 的 JSON 'null' 占位清理

背景 (2026-10-22 生产验证发现):
  V6.18 给 usage_logs.failed_channels 落失败链时, ORM 列声明是
  `Column(JSON)` —— SQLAlchemy 的 JSON 类型默认 none_as_null=False,
  Python None 会被序列化成 JSON 字面量 'null' 字符串 (sqlite typeof='text'),
  而不是 SQL NULL。

  后果: 每一行 usage_logs (含全部成功请求) 的 failed_channels 都存着
  字符串 'null' —— 将来任何 "failed_channels IS NOT NULL" / "!= ''" 的
  查询都会把成功请求算成"有失败链", 统计口径直接失真。

  修法 (两处):
  ① ORM 改 `Column(JSON(none_as_null=True))` —— None 落真 NULL
     (db/models.py, 无需 DDL 变更: sqlite 的 JSON 列底层本来就是 TEXT)
  ② 本迁移把已写入的 'null' 字符串行清理成 SQL NULL (幂等)

用法:
    python db/migrations/2026_10_22_v6x_failed_channels_null.py            # dry-run
    python db/migrations/2026_10_22_v6x_failed_channels_null.py --apply    # 实际执行
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    db_path = get_db_path()
    print(f"db path: {db_path}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # 只清 "字符串 'null'" 的行 — 真实失败链 (JSON 数组) 不动。
    # typeof()='text' 且 trim 后等于 'null' 才是占位符;
    # 真正的 SQL NULL 的 typeof() 是 'null' 而非 'text', 不会被误伤。
    print("\n=== 1. 扫描 failed_channels 占位符 ===")
    rows = conn.execute(
        "SELECT id, created_at, status_code, failed_channels FROM usage_logs "
        "WHERE typeof(failed_channels) = 'text' "
        "AND trim(failed_channels) = 'null' ORDER BY id",
    ).fetchall()
    print(f"  命中 {len(rows)} 行 (JSON 'null' 占位符)")
    for r in rows[:10]:
        print(f"    id={r['id']} created_at={r['created_at']} status={r['status_code']}")
    if len(rows) > 10:
        print(f"    ... 其余 {len(rows) - 10} 行省略")

    if args.apply:
        cur = conn.execute(
            "UPDATE usage_logs SET failed_channels = NULL "
            "WHERE typeof(failed_channels) = 'text' AND trim(failed_channels) = 'null'",
        )
        conn.commit()
        print(f"  [DONE] 已清理 {cur.rowcount} 行 → SQL NULL")
    else:
        print("  [DRY-RUN] 加 --apply 执行")

    # 校验: 清理后剩余的非 NULL 值应当都是真实 JSON 数组
    print("\n=== 2. 校验 ===")
    remain = conn.execute(
        "SELECT count(*) FROM usage_logs WHERE failed_channels IS NOT NULL",
    ).fetchone()[0]
    arrays = conn.execute(
        "SELECT count(*) FROM usage_logs WHERE failed_channels IS NOT NULL "
        "AND trim(failed_channels) LIKE '[%'",
    ).fetchone()[0]
    other = conn.execute(
        "SELECT count(*) FROM usage_logs WHERE failed_channels IS NOT NULL "
        "AND trim(failed_channels) NOT LIKE '[%'",
    ).fetchone()[0]
    print(f"  failed_channels IS NOT NULL: {remain} 行")
    print(f"    其中真实 JSON 数组: {arrays} 行")
    print(f"    其中非数组 (应为 0): {other} 行")

    conn.close()
    print("\n完成 (dry-run)" if not args.apply else "\n完成 (已执行)")


if __name__ == "__main__":
    main()
