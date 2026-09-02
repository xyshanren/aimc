"""Seed ModelGroup + ModelGroupMember 表 (V4.6.4, 2026-08-14)

V4.6.4 重设计 (post-merge):
- 6 个 tier group: flagship / strong / balanced / light / vision / code
- 删 scene:code (450499c 删了, 不重建) - 改用 tier:code 统一
- 删 scene:free (跟 tier:light 100% 成员重叠, 二选一)
- tier:vision require_vision=1, fallback 到 tier:strong
- tier:code 替代 scene:code, fallback 到 tier:balanced

设计:
- group_id 用 "tier:" 前缀统一 (跟 tier:flagship/strong/balanced/light 一套)
- 命名约定: 全部小写 + namespace 校验 (跟 db/normalizers.py 一致)
- member 顺序按 priority 升序 (越小越优先), 由 GROUP_MEMBERS 排序保证
- group_members 用 model_id 字符串 (soft join, 不强制 FK)
- 但 seed 时会校验 member 在 model_registry 里 (post-sync_siliconflow_to_registry.py)

依赖:
- scripts/seed_model_registry.py (Phase 2.2 seed 30 model) — 提供基础 model
- scripts/sync_siliconflow_to_registry.py --apply (V4.6.4) — 补全硅基流动 91 model
  (包含 glm-5.2 / qwen3-vl-32b-* / deepseek-v3.2 / deepseek-v4-pro 等)

用法:
    python scripts/seed_model_registry.py
    python scripts/sync_siliconflow_to_registry.py --apply
    python scripts/seed_model_groups.py
    重复运行会先清后插 (idempotent)

Fallback chain:
- tier:flagship -> tier:strong -> tier:balanced -> tier:light
- tier:strong   -> tier:balanced -> tier:light
- tier:balanced -> tier:light
- tier:vision   -> tier:strong
- tier:code     -> tier:balanced
- tier:light    -> NULL (终极兜底)

规模: 6 group + 22 group_member (V5.1: flagship 3 / strong 3 / balanced 3 / light 3 / vision 6 / code 4)
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import delete, select

from db.engine import async_session_factory
from db.init_db import init_db
from db.models import ModelGroup, ModelGroupMember, ModelRegistry
from db.normalizers import normalize_model_id, normalize_family


# 字段: (group_id, display_name, tier (限定), capabilities (JSON), min_context,
#        require_vision, strategy, fallback_group, sort_order)
GROUPS = [
    # ========== 4 个 tier 组 (能力档, 跟之前一致) ==========
    # tier:flagship 不限 tier (因为 deepseek-v4-pro 是 T0, glm-5.2 是 T1 但实际能力近 T0)
    # 用 min_context=64000 兜底
    (
        "tier:flagship", "旗舰推理组", None, None, 64000, 0, "cheapest", "tier:strong", 10,
    ),
    (
        # V5.1: tier T1 → NULL — 组内收 glm-5.3-flash (T2) 做免费兜底,
        # 精确 tier 过滤 (router.py:1310) 会把 T2 成员判死 (同类 bug 已在
        # tier:code / m2.7-highspeed 上发生 3 次); 组成员人工精选, 过滤器只添乱
        "tier:strong", "主力通用组", None, None, 0, 0, "cheapest", "tier:balanced", 20,
    ),
    (
        "tier:balanced", "均衡性价比组", "T2", None, 0, 0, "cheapest", "tier:light", 30,
    ),
    (
        "tier:light", "轻量免费组", "T3", None, 0, 0, "cheapest", None, 40,
    ),
    # ========== V4.6.4 新增 2 个 tier 组 ==========
    (
        # tier:vision: 多模态/视觉专用 (VLM)
        # require_vision=1, 路由会自动跳过 text-only model
        "tier:vision", "多模态视觉组", "T1", None, 0, 1, "cheapest", "tier:strong", 25,
    ),
    (
        # tier:code: 代码专用 (替代 scene:code, 统一 tier: 前缀)
        # V5.0: tier T2 -> NULL, capabilities 撤掉 — 精确 tier 过滤 (router.py:1310)
        # 把 T1 成员 (minimax-m2.7 / deepseek-v4-flash) 全判死, ["code"] caps 过滤
        # 把 deepseek-v3.2 (仅 ["general"]) 判死, 组内只剩 glm-5.3-flash 在工作。
        # 组成员本来就是人工精选的代码模型, 过滤器只添乱, 不如不限。
        "tier:code", "代码专用组", None, None, 0, 0, "cheapest", "tier:balanced", 50,
    ),
    # 注意: scene:code 和 scene:free 已删除 (V4.6.4)
    # - scene:code 跟 tier:code 行为一样, 用 tier: 前缀统一
    # - scene:free 跟 tier:light 100% 成员重叠, 选择 tier:light
]


# 字段: (group_id, model_id, priority, weight, max_cost_ratio, added_by)
# priority 数字越小越优先 (组内同价时 tie-break)
GROUP_MEMBERS = [
    # ========== tier:flagship (T0/T1 旗舰推理) ==========
    # deepseek-v4-pro (T0 评分 65.1; V5.0: sensenova 免费第二渠道, 官方付费兜底)
    # + kimi-k3 (V5.0: sensenova TokenPlan 免费渠道, T0, 原生视觉+1M ctx;
    #   modality=text 跟 minimax-m3 同例 — API 接受纯文本, 不进 vision 组
    #   否则被 require_vision 过滤判死; flagship 不限 vision, 带图请求自然落到它)
    # + glm-5.3 (V4.9.7: bigmodel coding plan Lite, 新一代旗舰)
    ("tier:flagship", "deepseek-v4-pro", 100, 1.0, None, "manual"),
    ("tier:flagship", "kimi-k3",         105, 1.0, None, "manual"),
    ("tier:flagship", "glm-5.3",         110, 1.0, None, "manual"),

    # ========== tier:strong (主力通用) ==========
    # deepseek-v4-flash (T1 评分 60.4) + glm-5.2 (T1, sensenova 免费渠道)
    # + glm-5.3-flash (T2, V5.1: bigmodel Lite 免费兜底 — strong 唯一 GLM Lite
    #   车道, 商汤 5h 窗口烧穿时组内仍有免费可用)
    # V5.1 调整: glm-5.3 移出 (Lite 池专心供 flagship, 5.3 积分系数是 flash 3 倍;
    #   此前 strong 85% 流量实际默认打 glm-5.3 — channel.priority=8 赢 0 元平价)
    # minimax-m3 移出 → tier:balanced 兜底 (能力低于 v4-flash/flash, TokenPlanMax
    #   年度额度大, 适合链路下游做容量缓冲: strong→balanced fallback 接住溢出)
    ("tier:strong", "deepseek-v4-flash", 100, 1.0, None, "manual"),
    ("tier:strong", "glm-5.2",           108, 1.0, None, "manual"),
    ("tier:strong", "glm-5.3-flash",     120, 1.0, None, "manual"),

    # ========== tier:balanced (T2 性价比) ==========
    # deepseek-v3 (T2, 硅基流动常驻, 低价付费)
    # + minimax-m3 (V5.1: T2 降档后入组, TokenPlanMax 年度额度管饱 —
    #   0 元 + priority 110 使其成为 balanced 日常默认, 把 sensenova 池
    #   省给 strong 的 v4-flash/glm-5.2; fallback 链 flagship→strong→balanced
    #   使其成为全链安全网)
    # + sensenova-6.8-flash-lite (T2, 公测免费, 256K ctx)
    # 注: glm-5.3-flash 不进 balanced — 保持 strong(+code) 双锚定即可,
    # 再进 balanced 会把主力组流量灌进 Lite 周池
    ("tier:balanced", "deepseek-v3",  100, 1.0, None, "manual"),
    ("tier:balanced", "minimax-m3",   110, 1.0, None, "manual"),
    ("tier:balanced", "sensenova-6.8-flash-lite", 120, 1.0, None, "manual"),

    # ========== tier:light (T3 轻量免费) ==========
    # V5.1: minimax 旧模型 (m2.7-highspeed/m2.5-highspeed) 移出 — 渠道只主推
    # m3/m2.7 两个新模型; m2.7-highspeed registry=T2 在 T3 组本就是死成员;
    # m2.5 同步移出 balanced。直连请求不受影响 (price_snapshots 保留)。
    # light 移除后无免费车道, 但 balanced 的 m3 (额度管饱) 兜底, fallback
    # 基本到不了 light
    ("tier:light", "qwen-qwen3-8b",   100, 1.0, None, "manual"),
    ("tier:light", "qwen-qwen3.5-4b", 110, 1.0, None, "manual"),
    ("tier:light", "glm-4-flash",     120, 1.0, None, "manual"),

    # ========== tier:vision (T1 多模态视觉, require_vision=1) ==========
    # qwen-vl 系列 (硅基流动常驻) + glm-4.5v (GLM 视觉)
    # + deepseek-v4-flash-vision-exp (V4.9.5: DeepSeek 视觉实验版)
    # V5.0: minimax-m3 移出 — V4.9.6 改 modality=text 后被 require_vision
    # 过滤 (router.py:1354 只认 vision/multimodal) 判成死成员, 留着只是噪音;
    # m3 的通用流量由 tier:strong 承接, 视觉需求由 vision-exp/glm-4.5v 承接
    # P2-A: qwen3-vl-* 改为 qwen-vl-* (validate_model_id 要求 model_id 以 "{family}-" 开头)
    ("tier:vision", "deepseek-v4-flash-vision-exp", 100, 1.0, None, "manual"),
    ("tier:vision", "qwen-vl-32b-instruct",     110, 1.0, None, "manual"),
    ("tier:vision", "qwen-vl-32b-thinking",     120, 1.0, None, "manual"),
    ("tier:vision", "qwen-vl-30b-a3b-thinking", 130, 1.0, None, "manual"),
    ("tier:vision", "qwen-vl-8b-instruct",      140, 1.0, None, "manual"),
    ("tier:vision", "glm-4.5v",                 150, 1.0, None, "manual"),

    # ========== tier:code (T2 代码专用, 替代 scene:code) ==========
    # minimax-m2.7 (T1 代码主力) + deepseek-v4-flash (含 code cap) + deepseek-v3.2
    # + glm-5.3-flash (V4.9.7: bigmodel coding plan Lite, 含 code cap, 兜底)
    ("tier:code", "minimax-m2.7",        100, 1.0, None, "manual"),
    ("tier:code", "deepseek-v4-flash",   110, 1.0, None, "manual"),
    ("tier:code", "deepseek-v3.2",       120, 1.0, None, "manual"),
    ("tier:code", "glm-5.3-flash",       130, 1.0, None, "manual"),

    # ========== 删的 group (历史, V4.6.4 不再 seed) ==========
    # - scene:code: 跟 tier:code 行为一样, 统一 tier: 前缀
    # - scene:free: 跟 tier:light 100% 成员重叠, 二选一保留 tier:light
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
            # 防御: group_id 必须归一化 (issue 1+2)
            gid_norm = group_id
            assert gid_norm == group_id, (
                f"GROUPS 元组里 group_id='{group_id}' 应已归一化"
            )
            grp = ModelGroup(
                group_id=gid_norm,
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
        print(f"[+] {group_count} 个 group")

        # 2) 查 model_registry (软校验, 缺则 warning)
        reg_models = {
            r.model_id: r.family
            for r in (await session.execute(
                select(ModelRegistry.model_id, ModelRegistry.family)
            )).all()
        }
        print(f"[*] model_registry 当前 {len(reg_models)} 个 model")

        # 3) seed members
        member_count = 0
        warned = 0
        for (
            group_id, model_id, priority, weight, max_cost_ratio, added_by,
        ) in GROUP_MEMBERS:
            # 防御: model_id 必须归一化 (跟 db/normalizers.py 一致)
            model_id_norm = normalize_model_id(model_id)
            assert model_id_norm == model_id, (
                f"GROUP_MEMBERS 元组里 model_id='{model_id}' "
                f"应已归一化为 '{model_id_norm}'"
            )

            # 软校验: model 是否在 registry 里
            if model_id not in reg_models:
                print(
                    f"  [!] {group_id}/{model_id} 不在 model_registry, "
                    f"请跑 scripts/sync_siliconflow_to_registry.py --apply 同步"
                )
                warned += 1
                continue

            mem = ModelGroupMember(
                group_id=group_id,
                model_id=model_id,
                priority=priority,
                weight=weight,
                max_cost_ratio=max_cost_ratio,
                added_by=added_by,
                is_active=1,
            )
            session.add(mem)
            member_count += 1
        await session.commit()

        if warned:
            print(
                f"\n[!] {warned} 个 member 未写入 (model 不在 registry), "
                f"请跑 sync_siliconflow_to_registry.py 同步后重试"
            )
        print(f"[+] {member_count} 个 member")


if __name__ == "__main__":
    print("=" * 60)
    print("AIMC seed_model_groups.py — V4.6.4 (6 个 tier group)")
    print("=" * 60)
    asyncio.run(seed())
    print("=" * 60)
    print("[OK] 完成")