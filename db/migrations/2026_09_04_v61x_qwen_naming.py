"""V6.x migration: qwen 命名规范化 (2026-09-04)

背景 (docs/next-major-v6-plan.md §4 V6.x, 用户决策):
- validate_model_id 旧规则强制 "{family}-" 前缀, 逼出两类坏名字:
  1. sync 预处理给 Qwen/Qwen3-8B 重复加 family → "qwen-qwen3-8b" (生产 43 个)
  2. 手工 seed 的 qwen3-vl 系列被迫丢版本号 → "qwen-vl-32b-instruct" (4 个)
- validate_model_id 已放宽 (接受 "{family}<版本数字/点>..."), sync 的
  convert_sf_model_id 已去冗余前缀 — 本迁移把存量数据改成同一规范。

改名规则 (只动这两类, qwen-max/qwen-plus/qwen-2.5-* 等合法名不动):
- "qwen-qwen<数字|.>..." → 去掉第一个 "qwen-"   (qwen-qwen3-8b → qwen3-8b)
- "qwen-vl-..."          → "qwen3-vl-..."       (qwen-vl-32b-instruct → qwen3-vl-32b-instruct)

冲突合并: sync 行与 seed 行是同一上游模型的重复条目 (字段一致, raw_ids 全空)。
改名后撞目标名时, 保留被 model_group_members 引用的行, 删除无引用的重复行;
usage_logs 历史按目标名合并 (成本审计口径连续)。

级联: model_registry.model_id / model_group_members.model_id /
usage_logs.model / price_snapshots.model (生产现状 0 行, 防御性包含)。
raw_model_id 不动 (上游原始名不受影响)。

用法:
    python db/migrations/2026_09_04_v61x_qwen_naming.py            # dry-run
    python db/migrations/2026_09_04_v61x_qwen_naming.py --apply   # 实际执行
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


def compute_new_name(mid: str) -> str | None:
    """返回规范化后的名字; 不需要改返回 None"""
    if mid.startswith("qwen-qwen"):
        rest = mid[len("qwen-"):]           # 第二段 "qwen3-8b" / "qwen3-vl-..."
        tail = rest[len("qwen"):]           # "3-8b" / ".5-4b"
        if tail[:1].isdigit() or tail[:1] == ".":
            return rest
        return None
    if mid.startswith("qwen-vl-"):
        return "qwen3-vl-" + mid[len("qwen-vl-"):]
    return None


def run(apply: bool) -> int:
    db_path = get_db_path()
    print(f"db path: {db_path}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    all_ids = {
        r[0] for r in conn.execute("SELECT model_id FROM model_registry")
    }
    member_ids = {
        r[0] for r in conn.execute(
            "SELECT DISTINCT model_id FROM model_group_members WHERE is_active = 1"
        )
    }

    renames: list[tuple[str, str]] = []   # (old, new)
    deletes: list[tuple[str, str]] = []   # (dup_old, kept_new) — 重复条目并入 kept
    for old in sorted(all_ids):
        new = compute_new_name(old)
        if new is None:
            continue
        if new in all_ids:
            # 目标名已被占 — 重复条目。保留被组引用的行; 若两行都没被引用,
            # 保留字典序较小 (稳定)。被保留行照常改名, 本行删除。
            occupant_has_members = new in member_ids
            old_has_members = old in member_ids
            if old_has_members and not occupant_has_members:
                deletes.append((new, old))     # 删占位行, old 改名顶上
                renames.append((old, new))
            else:
                deletes.append((old, new))     # old 并入占位行
            continue
        renames.append((old, new))

    print(f"\n待执行动作 ({len(renames)} 改名 + {len(deletes)} 重复合并):")
    for old, new in renames:
        tag = " [组内成员]" if old in member_ids else ""
        print(f"  - rename {old} -> {new}{tag}")
    for dup, kept in deletes:
        print(f"  - merge  {dup} -> {kept} (重复条目, 删除并并历史)")

    if not renames and not deletes:
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

        # 1) 重复合并: 历史 usage/prices 指向保留名, 然后删 registry 行
        for dup, kept in deletes:
            conn.execute(
                "UPDATE usage_logs SET model = ? WHERE model = ?",
                (kept, dup),
            )
            conn.execute(
                "UPDATE price_snapshots SET model = ? WHERE model = ?",
                (kept, dup),
            )
            conn.execute(
                "UPDATE model_group_members SET model_id = ? WHERE model_id = ?",
                (kept, dup),
            )
            conn.execute(
                "DELETE FROM model_registry WHERE model_id = ?",
                (dup,),
            )
            print(f"  [OK] merge {dup} -> {kept}")

        # 2) 改名 + 级联 (每行调用点内联字面量 + ? 绑定)
        for old, new in renames:
            conn.execute(
                "UPDATE model_registry SET model_id = ? WHERE model_id = ?",
                (new, old),
            )
            conn.execute(
                "UPDATE model_group_members SET model_id = ? WHERE model_id = ?",
                (new, old),
            )
            conn.execute(
                "UPDATE usage_logs SET model = ? WHERE model = ?",
                (new, old),
            )
            conn.execute(
                "UPDATE price_snapshots SET model = ? WHERE model = ?",
                (new, old),
            )
            print(f"  [OK] rename {old} -> {new}")

        conn.execute("COMMIT")
    except Exception as e:
        conn.execute("ROLLBACK")
        print(f"[FAIL] 执行失败, 已回滚: {e}")
        conn.close()
        raise

    # 终态确认
    remaining = conn.execute(
        "SELECT COUNT(*) FROM model_registry WHERE model_id LIKE 'qwen-qwen%'"
    ).fetchone()[0]
    print(f"\n[OK] 完成: 剩余 qwen-qwen* 双前缀行 = {remaining} (期望 0)")
    conn.close()
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    sys.exit(run(apply=args.apply))


if __name__ == "__main__":
    main()
