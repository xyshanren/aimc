"""修复 model_group_members.group_id 类型不一致 (V4.6.4 post-merge bug)

背景:
- 450499c commit 后, model_group_members.group_id 字段实际存两种值:
  * int 字符串 '1' '2' ... (跟 model_groups.id 对齐, soft FK)
  * string 'tier:flagship' 'scene:code' (跟 model_groups.group_id 对齐, string)
- gateway/router.py 用 string group_id (e.g. 'tier:flagship') 在 _group_members_cache 查,
  但 _group_members_cache 加载时用 m.group_id 字段 (int string), 永远查不到
- 症状: tier:flagship / tier:strong 等组路由返回 503

本脚本把 model_group_members.group_id 全部统一为 string, 跟 model_groups.group_id 对齐

用法:
    python scripts/fix_model_group_members_gid.py            # dry-run
    python scripts/fix_model_group_members_gid.py --apply   # 写 db
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from db.engine import async_session_factory
from db.init_db import init_db
from db.models import ModelGroup, ModelGroupMember
from sqlalchemy import select


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="实际写 db (默认 dry-run)")
    args = parser.parse_args()

    await init_db()

    async with async_session_factory() as s:
        # 1. 读 model_groups 的 id → group_id 映射
        groups = (await s.execute(select(ModelGroup))).scalars().all()
        id_to_gid = {g.id: g.group_id for g in groups}
        print("=== model_groups id -> group_id 映射 ===")
        for k, v in sorted(id_to_gid.items()):
            print(f"  {k} -> {v}")

        # 2. 扫 model_group_members
        members = (await s.execute(
            select(ModelGroupMember)
        )).scalars().all()
        print(f"\n=== model_group_members 总数: {len(members)} ===")

        # 3. 找需要改的 (group_id 是 int string, 不是 model_groups.group_id)
        valid_gids = set(id_to_gid.values())
        to_update = []
        for m in members:
            if m.group_id in valid_gids:
                continue  # 已经是 string
            # 尝试 int 转换
            try:
                int_gid = int(m.group_id)
                new_gid = id_to_gid.get(int_gid)
                if new_gid:
                    to_update.append((m.id, m.group_id, new_gid))
            except ValueError:
                # 不是 int 也不是 string group_id, 找不到对应, 跳过
                print(f"  [WARN] member id={m.id} group_id={m.group_id!r} 不在 model_groups")

        print(f"\n=== 需要更新: {len(to_update)} 行 ===")
        for mid, old, new in to_update:
            print(f"  id={mid}: {old!r} -> {new!r}")

        if not to_update:
            print("\n[OK] 无需更新, db 已经一致")
            return

        if not args.apply:
            print(f"\n[DRY-RUN] 加 --apply 才写 db")
            return

        # 4. 写 db
        for mid, old, new in to_update:
            await s.execute(
                ModelGroupMember.__table__.update()
                .where(ModelGroupMember.id == mid)
                .values(group_id=new)
            )
        await s.commit()
        print(f"\n[OK] 更新 {len(to_update)} 行")


if __name__ == "__main__":
    asyncio.run(main())
