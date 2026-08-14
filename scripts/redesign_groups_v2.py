"""重设计 model group 路由 (V4.6.4 post-merge)

V4.6.4 后 model_id 全部小写归一化, 但 model_group_members 仍引用旧大写/已删 model,
导致 router cache 503. 本脚本:
1. 清空 model_group_members 所有行
2. 重建 7 个 model group + 27 个 member, 全部用 siliconflow 真实可用的 model

设计:
- tier:flagship (T1) - 重活/规划/设计/审核 (主力: minimax-m3)
- tier:strong (T1/T2) - 主力通用 (hermes 主代理, 主力: minimax-m2.7)
- tier:balanced (T2) - 性价比 (主力: deepseek-v3 + minimax-m2.5)
- tier:light (T2/T3) - 轻量 (主力: minimax-m2.7-highspeed + qwen3-8b)
- tier:vision (T1) - 多模态视觉 (minimax-m3 + qwen3-vl 系列)
- tier:code (T2) - 代码 (主力: minimax-m2 + deepseek-v4-pro)
- scene:free (T3) - 免费 (主力: qwen3.5-4b + qwen3-8b)

Fallback chain:
- tier:flagship -> tier:strong -> tier:balanced -> tier:light
- tier:strong -> tier:balanced -> tier:light
- tier:balanced -> tier:light
- tier:vision -> tier:strong
- tier:code -> tier:balanced

依赖:
- 依赖 sync_siliconflow_to_registry.py --apply 同步 91 个 model 到 model_registry
- 依赖 fix_model_group_members_gid.py (group_id 统一为 string)

用法:
    python scripts/redesign_groups_v2.py            # dry-run
    python scripts/redesign_groups_v2.py --apply   # 写 db
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
from sqlalchemy import delete, select

# (group_id, display_name, strategy, fallback_group, sort_order, require_vision)
NEW_GROUPS = [
    ("tier:flagship",  "旗舰推理组",   "cheapest", "tier:strong",    10, False),
    ("tier:strong",    "主力通用组",   "cheapest", "tier:balanced",  20, False),
    ("tier:balanced",  "均衡性价比组", "cheapest", "tier:light",     30, False),
    ("tier:light",     "轻量免费组",   "cheapest", None,              40, False),
    ("tier:vision",    "多模态视觉组", "cheapest", "tier:strong",    25, True),
    ("tier:code",      "代码专用组",   "cheapest", "tier:balanced",  50, False),
    ("scene:free",     "零成本组",     "priority", None,              60, False),
]

# (model_id, priority) 数字越小越优先
GROUP_MEMBERS = {
    "tier:flagship": [
        ("minimax-m3", 100),
        ("deepseek-deepseek-v4-pro", 110),
        ("minimax-m2.7", 120),
        ("qwen-qwen3-vl-32b-thinking", 130),
    ],
    "tier:strong": [
        ("minimax-m2.7", 100),
        ("deepseek-deepseek-v4-flash", 110),
        ("minimax-m2.5", 120),
        ("qwen-qwen3-vl-32b-instruct", 130),
    ],
    "tier:balanced": [
        ("deepseek-deepseek-v3", 100),
        ("minimax-m2.5", 110),
        ("minimax-m2.1", 120),
        ("minimax-m2.1-highspeed", 130),
    ],
    "tier:light": [
        ("minimax-m2.7-highspeed", 100),
        ("qwen-qwen3-8b", 110),
        ("qwen-qwen3.5-4b", 120),
        ("minimax-m2.5-highspeed", 130),
    ],
    "tier:vision": [
        ("minimax-m3", 100),
        ("qwen-qwen3-vl-32b-thinking", 110),
        ("qwen-qwen3-vl-30b-a3b-thinking", 120),
        ("qwen-qwen3-vl-8b-instruct", 130),
    ],
    "tier:code": [
        ("minimax-m2", 100),
        ("deepseek-deepseek-v4-pro", 110),
        ("deepseek-deepseek-v4-flash", 120),
        ("deepseek-deepseek-v3.2", 130),
    ],
    "scene:free": [
        ("qwen-qwen3.5-4b", 100),
        ("qwen-qwen3-8b", 110),
        ("minimax-m2.5-highspeed", 120),
    ],
}

# 旧 group (要清空 members, 但保留 group 本身)
OLD_TIER_GROUPS = ["tier:flagship", "tier:strong", "tier:balanced", "tier:light"]
# 整个删除的 group (不再用)
GROUPS_TO_DELETE = ["scene:code", "scene:free"]


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    await init_db()
    print("=== 重设计 model groups ===\n")
    async with async_session_factory() as s:
        # 1. 删整个旧 group (scene:code, scene:free)
        for gid in GROUPS_TO_DELETE:
            r = await s.execute(select(ModelGroup).where(ModelGroup.group_id == gid))
            g = r.scalars().first()
            if g:
                await s.execute(delete(ModelGroupMember).where(ModelGroupMember.group_id == g.id))
                await s.delete(g)
                print(f"  [DEL] group {gid}")
        # 2. 清空旧 tier:* members
        for gid in OLD_TIER_GROUPS:
            r = await s.execute(select(ModelGroup).where(ModelGroup.group_id == gid))
            g = r.scalars().first()
            if g:
                await s.execute(delete(ModelGroupMember).where(ModelGroupMember.group_id == g.id))
                print(f"  [CLR] {gid} members")
        # 3. 创建/更新新 group
        existing_ids = {r.group_id for r in (await s.execute(select(ModelGroup))).scalars()}
        for gid, name, strategy, fallback, sort, vision in NEW_GROUPS:
            if gid in OLD_TIER_GROUPS:
                r = await s.execute(select(ModelGroup).where(ModelGroup.group_id == gid))
                g = r.scalars().first()
                g.require_vision = 1 if vision else 0
                g.fallback_group = fallback
                g.strategy = strategy
                g.sort_order = sort
                print(f"  [UPD] {gid}: require_vision={vision}, fallback={fallback}")
            elif gid in existing_ids:
                print(f"  [SKIP] {gid} 已存在")
            else:
                s.add(ModelGroup(
                    group_id=gid,
                    display_name=name,
                    tier=None,
                    capabilities=None,
                    min_context=None,
                    require_vision=1 if vision else 0,
                    strategy=strategy,
                    fallback_group=fallback,
                    enabled=1,
                    sort_order=sort,
                ))
                print(f"  [ADD] {gid}")
        # 4. 重建所有 group members
        for gid, members in GROUP_MEMBERS.items():
            r = await s.execute(select(ModelGroup).where(ModelGroup.group_id == gid))
            g = r.scalars().first()
            if not g:
                print(f"  [WARN] {gid} group 不存在, 跳过 members")
                continue
            for mid, pri in members:
                s.add(ModelGroupMember(
                    group_id=g.id,
                    model_id=mid,
                    priority=pri,
                    weight=1.0,
                    added_by="manual",
                    is_active=1,
                ))
            print(f"  [ADD] {gid}: {len(members)} members")
        await s.commit()
    print("\n[OK] 全部完成")


if __name__ == "__main__":
    asyncio.run(main())
