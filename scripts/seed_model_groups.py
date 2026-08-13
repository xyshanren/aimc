"""Seed ModelGroup + ModelGroupMember 表 (Phase 2.7, 2026-07-29)

存模型组定义 + 组成员关联。

设计 (跟 phase2-design.md §3.2/3.3):
- 6 个 group: 4 tier (flagship/strong/balanced/light) + 2 scene (code/free)
- group_members 用 model_id 字符串 (soft join, 不强制 FK)
- tier: 限定能力档 (NULL=不限)
- fallback_group: 降级目标 (NULL=无降级, 终极兜底)
- strategy: cheapest (默认) / priority
- min_context / require_vision: 可选过滤

关联 model_registry (Phase 2.2 seed 25 个 model):
- tier:flagship: 强推理/长上下文/代码旗舰
- tier:strong: 主力通用
- tier:balanced: 性价比
- tier:light: 轻量
- scene:code: 代码专用 (caps=['code'])
- scene:free: 零成本 (caps 含 'free' — 但当前 registry 没这个 cap, 用 min_context=0 兜底)

注 (2026-07-29):
- 当前 DB 只有 1 个 channel (siliconflow_aggregator), 实际组路由
  走 select_best_channel → fallback 兜底
- 等用户申请 8 个直连 API key 后, 组路由才能真正跨 channel 选优
- model_id 命名跟硅基流动的 91 个 model 名不一定一致, 短期组路由
  会 fallback 到硅基流动 (如果硅基流动不识别 model 名, 会 404)

用法:
    python scripts/seed_model_groups.py
    重复运行会先清后插 (idempotent, 跟 seed_model_cards.py / seed_model_registry.py 风格一致)

规模: 6 group + 24 group_member
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import delete, select

from db.engine import async_session_factory
from db.init_db import init_db
from db.models import ModelGroup, ModelGroupMember, ModelRegistry
from db.normalizers import normalize_model_id, validate_model_id


# 字段: (group_id, display_name, tier (限定), capabilities (JSON), min_context,
#        require_vision, strategy, fallback_group, sort_order)
GROUPS = [
    # ========== 4 个 tier 组 (能力档) ==========
    # tier:flagship 不限 tier (因为 qwen-max/MiniMax-Text-01 在 registry 是 T1 但实际能力近 T0)
    # 用 min_context=64000 兜底 (4 个 member 全部 >= 64k)
    (
        "tier:flagship", "旗舰推理组", None, None, 64000, 0, "cheapest", "tier:strong", 10,
    ),
    (
        "tier:strong", "强力通用组", "T1", None, 0, 0, "cheapest", "tier:balanced", 20,
    ),
    (
        "tier:balanced", "均衡性价比组", "T2", None, 0, 0, "cheapest", "tier:light", 30,
    ),
    (
        "tier:light", "轻量免费组", "T3", None, 0, 0, "cheapest", None, 40,
    ),
    # ========== 2 个 scene 组 (使用场景) ==========
    (
        "scene:code", "代码专用组", None, '["code"]', 0, 0, "cheapest", "tier:balanced", 50,
    ),
    (
        "scene:free", "零成本组", "T3", None, 0, 0, "priority", None, 60,
    ),
]


# 字段: (group_id, model_id, priority, weight, max_cost_ratio, added_by)
GROUP_MEMBERS = [
    # ========== tier:flagship (T0) ==========
    # Phase 2a+ fix (2026-08-13): deepseek-reasoner → deepseek-v4-pro, 新增 MiniMax-M2.7
    ("tier:flagship", "deepseek-v4-pro", 100, 1.0, None, "manual"),
    ("tier:flagship", "kimi-k3", 100, 1.0, None, "manual"),
    ("tier:flagship", "qwen-max", 110, 1.0, None, "manual"),
    ("tier:flagship", "minimax-m2.7", 120, 1.0, None, "manual"),

    # ========== tier:strong (T1) ==========
    # Phase 2a+ fix (2026-08-13): deepseek-chat / deepseek-coder → deepseek-v4-flash
    ("tier:strong", "deepseek-v4-flash", 100, 1.0, None, "manual"),
    ("tier:strong", "glm-4-plus", 100, 1.0, None, "manual"),
    ("tier:strong", "kimi-k2", 100, 1.0, None, "manual"),
    ("tier:strong", "qwen-2.5-72b", 110, 1.0, None, "manual"),
    ("tier:strong", "doubao-pro-32k", 120, 1.0, None, "manual"),
    ("tier:strong", "hunyuan-pro", 130, 1.0, None, "manual"),

    # ========== tier:balanced (T2) ==========
    # Phase 2a+ fix (2026-08-13): 移除 deepseek-v2.5 (registry 已删)
    ("tier:balanced", "qwen-plus", 100, 1.0, None, "manual"),
    ("tier:balanced", "qwen-2.5-32b", 110, 1.0, None, "manual"),
    ("tier:balanced", "glm-4-air", 100, 1.0, None, "manual"),
    ("tier:balanced", "hunyuan-standard", 120, 1.0, None, "manual"),
    ("tier:balanced", "llama-3.1-70b", 130, 1.0, None, "manual"),
    ("tier:balanced", "step-1-8k", 140, 1.0, None, "manual"),

    # ========== tier:light (T3) ==========
    ("tier:light", "qwen-turbo", 100, 1.0, None, "manual"),
    ("tier:light", "qwen-2.5-7b", 110, 1.0, None, "manual"),
    ("tier:light", "glm-4-flash", 100, 1.0, None, "manual"),
    ("tier:light", "glm-4-9b", 110, 1.0, None, "manual"),
    ("tier:light", "llama-3.1-8b", 120, 1.0, None, "manual"),
    ("tier:light", "moonshot-v1-8k", 130, 1.0, None, "manual"),
    ("tier:light", "doubao-lite-32k", 140, 1.0, None, "manual"),

    # ========== scene:code (T1+ 含 code cap) ==========
    # Phase 2a+ fix (2026-08-13): deepseek-coder → deepseek-v4-flash (含 code cap)
    ("scene:code", "deepseek-v4-flash", 100, 1.0, None, "manual"),
    ("scene:code", "qwen-max", 100, 1.0, None, "manual"),
    ("scene:code", "glm-4-plus", 100, 1.0, None, "manual"),
    ("scene:code", "hunyuan-pro", 100, 1.0, None, "manual"),

    # ========== scene:free (T3, priority 策略) ==========
    ("scene:free", "qwen-turbo", 100, 1.0, None, "manual"),
    ("scene:free", "qwen-2.5-7b", 110, 1.0, None, "manual"),
    ("scene:free", "glm-4-flash", 100, 1.0, None, "manual"),
    ("scene:free", "glm-4-9b", 110, 1.0, None, "manual"),
    ("scene:free", "llama-3.1-8b", 120, 1.0, None, "manual"),
    ("scene:free", "moonshot-v1-8k", 130, 1.0, None, "manual"),
]


async def seed():
    await init_db()

    async with async_session_factory() as session:
        # 幂等: 先清后插
        await session.execute(delete(ModelGroupMember))
        await session.execute(delete(ModelGroup))
        await session.commit()

        # 1) seed groups
        group_count = 0
        for (
            group_id, display_name, tier, capabilities,
            min_context, require_vision, strategy, fallback_group, sort_order,
        ) in GROUPS:
            grp = ModelGroup(
                group_id=group_id,
                display_name=display_name,
                tier=tier,
                capabilities=capabilities,
                min_context=min_context,
                require_vision=require_vision,
                strategy=strategy,
                fallback_group=fallback_group,
                enabled=1,
                sort_order=sort_order,
            )
            session.add(grp)
            group_count += 1
        await session.commit()

        # 2) seed members
        member_count = 0
        # Phase 4 v4.3 (2026-08-13): 校验 member model_id 已在 model_registry 里 (防漂移)
        reg_models = {
            r.model_id: r.family
            for r in (await session.execute(select(ModelRegistry.model_id, ModelRegistry.family))).all()
        }
        for (
            group_id, model_id, priority, weight, max_cost_ratio, added_by,
        ) in GROUP_MEMBERS:
            # 归一化 + 校验 namespace
            model_id_norm = normalize_model_id(model_id)
            assert model_id_norm == model_id, (
                f"GROUP_MEMBERS 元组里 model_id='{model_id}' 应已归一化为 '{model_id_norm}'"
            )
            # 必须在 registry 里有同名 model_id
            assert model_id_norm in reg_models, (
                f"GROUP_MEMBERS [{group_id}] model_id='{model_id_norm}' 不在 model_registry 中, "
                f"应先在 seed_model_registry.py 加这个 model"
            )
            # namespace 检查 (按 registry 的 family)
            family = reg_models[model_id_norm]
            assert validate_model_id(model_id_norm, family), (
                f"GROUP_MEMBERS [{group_id}] model_id='{model_id_norm}' namespace 与 family='{family}' 不匹配"
            )
            mem = ModelGroupMember(
                group_id=group_id,
                model_id=model_id_norm,
                priority=priority,
                weight=weight,
                max_cost_ratio=max_cost_ratio,
                added_by=added_by,
            )
            session.add(mem)
            member_count += 1
        await session.commit()

        # 验证
        grp_rows = list((await session.execute(
            select(ModelGroup).order_by(ModelGroup.sort_order)
        )).scalars().all())
        mem_rows = list((await session.execute(
            select(ModelGroupMember).order_by(ModelGroupMember.group_id, ModelGroupMember.priority)
        )).scalars().all())

        print(f"\n=== Seeded {group_count} model groups, {member_count} group members ===")
        print("\n--- Groups ---")
        for g in grp_rows:
            caps_str = g.capabilities or "[]"
            print(
                f"  {g.group_id:20s}  tier={g.tier or '*':3s}  "
                f"strategy={g.strategy:9s}  fallback={g.fallback_group or '-':20s}  "
                f"caps={caps_str:20s}  members={sum(1 for m in mem_rows if m.group_id == g.group_id)}"
            )

        print("\n--- Members (per group) ---")
        for g in grp_rows:
            members = [m for m in mem_rows if m.group_id == g.group_id]
            if members:
                print(f"  [{g.group_id}]")
                for m in members:
                    reg = await session.get(ModelRegistry, m.model_id) if False else None
                    # 用 query 代替
                    reg_q = await session.execute(
                        select(ModelRegistry).where(ModelRegistry.model_id == m.model_id)
                    )
                    reg = reg_q.scalar_one_or_none()
                    tier_str = f"({reg.tier})" if reg else "(missing!)"
                    print(f"    {m.model_id:25s}  {tier_str:6s}  priority={m.priority}")


if __name__ == "__main__":
    asyncio.run(seed())
