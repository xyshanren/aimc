"""V6.17d migration: 模型组语义准入 (tier:code 代码特化 + tier:vision 位次回调)

背景 (用户 2026-10-21 拍板):
  看板「加成员」下拉此前在前端硬编码 tier 白名单, 与路由层各走各的 ——
  ① 能选到运行时会被跳过的死成员 (tier:vision require_vision=1, 纯文本模型
     入组后运行时直接 skip);
  ② 选不到该选的 (flagship min_context=64000 曾能选上下文不足的模型);
  ③ 组语义 (视觉组只收多模态 / 代码组只收代码特化) 完全没有约束。
  另: 加成员时 priority 自动取 max+5 (minimax-m3.1-flash 加回 tier:vision 拿到
  125, 而它当初在 vision 是 90), 且 UI 不可改。

改动 (幂等收敛):
1. model_registry: 代码特化模型补 capability 标签 **code_specialized**
   (kimi-k2.7-code / qwen3-coder-30b-a3b-instruct / kimi-k2.7-code-highspeed) ——
   泛用模型即便 capabilities 含 'code' 也不算特化 ("会写代码" ≠ "代码专用")
2. model_groups: tier:code.capabilities = ["code_specialized"] (运行时强制)
   —— 顺序在 1 之后, 保证组内现有成员不被过滤掉
3. model_group_members: tier:vision 的 minimax-m3.1-flash priority 125 → 90
   (沿用 -preview 旧行的排位, 恢复它作为视觉组首选的策展意图)

⚠️ 代码已上线 (gateway/group_rules.group_runtime_accepts): tier:code 组要求
   code_specialized 后, 缺该标签的成员会被路由跳过 —— 本迁移先补标签再改组约束。

用法:
    python db/migrations/2026_10_21_v6x_group_specialization.py            # dry-run
    python db/migrations/2026_10_21_v6x_group_specialization.py --apply    # 实际执行
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

CODE_GROUP_ID = "tier:code"
CODE_GROUP_CAPS = ["code_specialized"]

# 模型 → 需具备的 capability 标签 (只增不减)
CODE_SPECIALIZED_MODELS = {
    "kimi-k2.7-code": ["code", "code_specialized"],
    "qwen3-coder-30b-a3b-instruct": ["code", "code_specialized"],
    "kimi-k2.7-code-highspeed": ["code", "code_specialized"],
}

# tier:vision 位次回调 (模型, 目标 priority)
VISION_PRIORITY_FIX = ("minimax-m3.1-flash", 90)


def get_db_path() -> str:
    import config
    db_path = config.settings.db_path
    if db_path == ":memory:":
        return "/tmp/test_aimc.db"
    return db_path


def _parse_caps(raw) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [str(x) for x in parsed] if isinstance(parsed, list) else []


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    db_path = get_db_path()
    print(f"db path: {db_path}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # ============ 1. 代码特化模型补 code_specialized 标签 ============
    print("\n=== 1. model_registry 代码特化标签 ===")
    projected_caps: dict[str, list[str]] = {}
    for model_id, need in CODE_SPECIALIZED_MODELS.items():
        row = conn.execute(
            "SELECT model_id, capabilities FROM model_registry WHERE model_id = ?",
            (model_id,),
        ).fetchone()
        if not row:
            print(f"  [SKIP] {model_id} 不在 model_registry")
            continue
        caps = _parse_caps(row["capabilities"])
        missing = [c for c in need if c not in caps]
        if not missing:
            print(f"  [OK] {model_id} caps={caps} 已含 {need}")
            projected_caps[model_id] = caps
            continue
        new_caps = caps + missing
        print(f"  [TO-ADD] {model_id}: +{missing} → {new_caps}")
        projected_caps[model_id] = new_caps
        if args.apply:
            conn.execute(
                "UPDATE model_registry SET capabilities = ? WHERE model_id = ?",
                (json.dumps(new_caps, ensure_ascii=False), model_id),
            )

    if args.apply:
        conn.commit()

    # ============ 2. tier:code 组能力 = code_specialized ============
    print("\n=== 2. model_groups tier:code 能力约束 ===")
    grp = conn.execute(
        "SELECT group_id, capabilities FROM model_groups WHERE group_id = ?",
        (CODE_GROUP_ID,),
    ).fetchone()
    if not grp:
        print(f"  [FAIL] 组 {CODE_GROUP_ID} 不存在, 中止")
        conn.close()
        return
    cur_caps = _parse_caps(grp["capabilities"])
    if set(CODE_GROUP_CAPS).issubset(set(cur_caps)):
        print(f"  [OK] {CODE_GROUP_ID}.capabilities = {cur_caps} 已满足")
    else:
        # 前置校验: 组内至少要有一个活跃成员满足新约束, 否则组会空
        # (dry-run 用 step 1 的**投影**能力标签判定 —— 标签补上后才会成立)
        members = conn.execute(
            "SELECT m.model_id, r.capabilities FROM model_group_members m "
            "LEFT JOIN model_registry r ON r.model_id = m.model_id "
            "WHERE m.group_id = ? AND m.is_active = 1",
            (CODE_GROUP_ID,),
        ).fetchall()
        ok_members = [
            m["model_id"] for m in members
            if set(CODE_GROUP_CAPS).issubset(
                set(projected_caps.get(m["model_id"])
                    or _parse_caps(m["capabilities"])))
        ]
        if not ok_members:
            print(f"  [FAIL] {CODE_GROUP_ID} 无满足 {CODE_GROUP_CAPS} 的活跃成员 "
                  f"(成员: {[m['model_id'] for m in members]}), 中止 — "
                  f"先补 capability 标签再改组约束")
            conn.close()
            return
        print(f"  [TO-SET] {CODE_GROUP_ID}.capabilities: {cur_caps} → {CODE_GROUP_CAPS} "
              f"(满足约束的成员: {ok_members})")
        if args.apply:
            conn.execute(
                "UPDATE model_groups SET capabilities = ? WHERE group_id = ?",
                (json.dumps(CODE_GROUP_CAPS, ensure_ascii=False), CODE_GROUP_ID),
            )
            conn.commit()

    # ============ 3. tier:vision minimax-m3.1-flash 位次回调 ============
    print("\n=== 3. model_group_members tier:vision 位次 ===")
    model_id, target = VISION_PRIORITY_FIX
    mem = conn.execute(
        "SELECT id, priority, is_active FROM model_group_members "
        "WHERE group_id = 'tier:vision' AND model_id = ?",
        (model_id,),
    ).fetchone()
    if not mem:
        print(f"  [NONE] tier:vision 无 {model_id} 成员行, 跳过")
    elif mem["priority"] == target:
        print(f"  [OK] {model_id} priority 已是 {target}")
    else:
        print(f"  [TO-SET] {model_id} priority {mem['priority']} → {target}")
        if args.apply:
            conn.execute(
                "UPDATE model_group_members SET priority = ? WHERE id = ?",
                (target, mem["id"]),
            )
            conn.commit()

    # ============ 4. 收尾校验 ============
    print("\n=== 4. 校验 ===")
    for gid in ("tier:code", "tier:vision"):
        rows = conn.execute(
            "SELECT m.model_id, m.priority, r.modality, r.capabilities "
            "FROM model_group_members m LEFT JOIN model_registry r ON r.model_id = m.model_id "
            "WHERE m.group_id = ? AND m.is_active = 1 ORDER BY m.priority",
            (gid,),
        ).fetchall()
        print(f"  {gid}: {len(rows)} 个活跃成员")
        for r in rows:
            print(f"     {r['model_id']:<34} pri={r['priority']} "
                  f"modality={r['modality']} caps={r['capabilities']}")

    conn.close()
    print("\n完成 (dry-run)" if not args.apply else "\n完成 (已执行)")


if __name__ == "__main__":
    main()
