"""Seed ModelRegistry 表 (Phase 2.2, 2026-07-29)

存模型的 **路由元信息**（tier / capabilities / context_length / modality），
跟 model_cards（benchmark 引用分）双轨，model_id 字符串 soft join。

设计：
- tier/capabilities 是路由"硬"字段（决策依据）
- benchmark_scores 仅 Dashboard 展示用（Phase 2.9 才用），不参与路由
- capabilities 是 JSON 字符串，应用层 json.loads
- model_id 规范: {family}-{variant}，如 deepseek-chat / qwen-plus / kimi-k3

来源（2026-07-29）：
- 已配 channel 实际支持的 model（直连 + 硅基流动）
- architecture.md §九 预置模型组配置
- 实际能力定位：tier 反映"在该 family 内的能力档"

用法：
    python scripts/seed_model_registry.py
    重复运行会先清后插 (idempotent, 跟 seed_model_cards.py 风格一致)

规模：25+ 个 model (V4.6.4 +6 个 SenseNova 相关)
覆盖厂商：deepseek / qwen / glm / kimi / doubao / hunyuan / stepfun / sensenova / MiniMax / llama
覆盖 tier：T0 / T1 / T2 / T3
覆盖 capabilities：reasoning / code / math / general / long_context / agent / multimodal
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import delete, select

from db.engine import async_session_factory
from db.init_db import init_db
from db.models import ModelRegistry


# 字段：model_id, family, display_name, tier, capabilities (list), context_length,
#       modality, is_open_source, release_date, notes
MODEL_REGISTRY = [
    # ========== DeepSeek (4 个) ==========
    (
        "deepseek-reasoner", "deepseek", "DeepSeek Reasoner", "T0",
        ["reasoning", "math", "code"], 64000, "text", 0, "2025-01",
        "R1 推理模型, 强推理/数学/代码"
    ),
    (
        "deepseek-chat", "deepseek", "DeepSeek V3 Chat", "T1",
        ["reasoning", "code", "general"], 128000, "text", 0, "2025-12",
        "V3 主力, 128k ctx, 综合能力强"
    ),
    (
        "deepseek-coder", "deepseek", "DeepSeek Coder", "T1",
        ["code"], 64000, "text", 1, "2024-06",
        "代码专用, 老牌但仍强"
    ),
    (
        "deepseek-v2.5", "deepseek", "DeepSeek V2.5", "T2",
        ["general"], 128000, "text", 0, "2024-09",
        "V2.5 上一代, 仍在服务"
    ),

    # ========== Qwen (6 个) ==========
    (
        "qwen-max", "qwen", "Qwen Max", "T1",
        ["reasoning", "code", "general"], 128000, "text", 0, "2025-04",
        "通义主力旗舰"
    ),
    (
        "qwen-plus", "qwen", "Qwen Plus", "T2",
        ["general"], 128000, "text", 0, "2025-04",
        "通义主力, 性价比"
    ),
    (
        "qwen-turbo", "qwen", "Qwen Turbo", "T3",
        ["general"], 128000, "text", 0, "2025-04",
        "通义极速版, 便宜"
    ),
    (
        "qwen2.5-72b", "qwen", "Qwen 2.5 72B", "T1",
        ["reasoning", "code", "general"], 128000, "text", 1, "2024-09",
        "Qwen 2.5 72B 开源旗舰, 硅基流动常驻"
    ),
    (
        "qwen2.5-32b", "qwen", "Qwen 2.5 32B", "T2",
        ["general"], 128000, "text", 1, "2024-09",
        "Qwen 2.5 32B 开源, 性价比"
    ),
    (
        "qwen2.5-7b", "qwen", "Qwen 2.5 7B", "T3",
        ["general"], 32000, "text", 1, "2024-09",
        "Qwen 2.5 7B 轻量"
    ),

    # ========== GLM (4 个) ==========
    (
        "glm-4-plus", "glm", "GLM-4 Plus", "T1",
        ["reasoning", "code", "general"], 128000, "text", 0, "2025-01",
        "智谱旗舰闭源, 主力"
    ),
    (
        "glm-4-air", "glm", "GLM-4 Air", "T2",
        ["general"], 128000, "text", 0, "2024-12",
        "智谱性价比"
    ),
    (
        "glm-4-flash", "glm", "GLM-4 Flash", "T3",
        ["general", "agent"], 128000, "text", 0, "2024-12",
        "智谱极速, Agent 调优"
    ),
    (
        "glm-4-9b", "glm", "GLM-4 9B", "T3",
        ["general"], 32000, "text", 1, "2024-06",
        "智谱开源轻量, 硅基流动常驻"
    ),

    # ========== Kimi (2 个) ==========
    (
        "kimi-k2", "kimi", "Kimi K2", "T1",
        ["reasoning", "long_context", "general"], 200000, "text", 0, "2025-07",
        "月之暗面 K2 闭源, 200k 长上下文"
    ),
    (
        "kimi-k3", "kimi", "Kimi K3", "T0",
        ["reasoning", "long_context", "code"], 1000000, "text", 0, "2026-07",
        "K3 最新旗舰, 1M ctx, 2026-07-27 开源权重"
    ),
    (
        "moonshot-v1-8k", "kimi", "Moonshot V1 8K", "T3",
        ["general"], 8000, "text", 0, "2024-08",
        "Moonshot 短上下文"
    ),

    # ========== Doubao (2 个) ==========
    (
        "doubao-pro-32k", "doubao", "Doubao Pro 32K", "T1",
        ["reasoning", "general"], 32000, "text", 0, "2025-02",
        "字节豆包旗舰"
    ),
    (
        "doubao-lite-32k", "doubao", "Doubao Lite 32K", "T3",
        ["general"], 32000, "text", 0, "2025-02",
        "字节豆包轻量"
    ),

    # ========== Hunyuan (2 个) ==========
    (
        "hunyuan-pro", "hunyuan", "Hunyuan Pro", "T1",
        ["reasoning", "code", "general"], 32000, "text", 0, "2025-04",
        "腾讯混元旗舰"
    ),
    (
        "hunyuan-standard", "hunyuan", "Hunyuan Standard", "T2",
        ["general"], 32000, "text", 0, "2025-04",
        "腾讯混元标准"
    ),

    # ========== StepFun (1 个) ==========
    (
        "step-1-8k", "stepfun", "Step-1 8K", "T2",
        ["general"], 8000, "text", 0, "2024-10",
        "阶跃星辰基础"
    ),

    # ========== SenseNova (4 个, V4.6.4) ==========
    # sensenova-6.7-flash-lite: 8/31 后官方重定向失效, 标记 deprecated
    (
        "sensenova-6.7-flash-lite", "sensenova", "SenseNova 6.7 Flash Lite", "T2",
        ["general", "multimodal"], 128000, "multimodal", 0, "2026-05",
        "商汤日日新 6.7 flash-lite, 8/31 后官方重定向失效 (deprecated)"
    ),
    # sensenova-6.8-flash-lite: 当前公测 token plan 主力 (替代 6.7)
    (
        "sensenova-6.8-flash-lite", "sensenova", "SenseNova 6.8 Flash Lite", "T1",
        ["general", "multimodal"], 128000, "multimodal", 0, "2026-08",
        "商汤日日新 6.8 flash-lite, 公测 token plan 主力 (8/8 后取代 6.7)"
    ),
    # sensenova-u1-fast: U1 加速版, Neo-unify 架构
    (
        "sensenova-u1-fast", "sensenova", "SenseNova U1 Fast", "T1",
        ["general", "multimodal"], 128000, "multimodal", 0, "2026-08",
        "商汤日日新 U1 加速版, Neo-unify 架构, 公测 token plan"
    ),
    # deepseek-v4-flash: 商汤渠道公测免费提供 (deepseek family, sensenova channel)
    (
        "deepseek-v4-flash", "deepseek", "DeepSeek V4 Flash", "T1",
        ["reasoning", "code", "general"], 1000000, "text", 0, "2026-07",
        "DeepSeek V4 Flash, 1M ctx, 思考模式+工具调用, 商汤公测 token plan 免费"
    ),

    # ========== MiniMax (1 个) ==========
    (
        "MiniMax-Text-01", "MiniMax", "MiniMax Text-01", "T1",
        ["long_context", "general"], 1000000, "text", 0, "2025-04",
        "MiniMax 长上下文, 1M ctx, 实际用 MiniMax-Text-01"
    ),

    # ========== Llama (2 个) ==========
    (
        "llama-3.1-70b", "llama", "Llama 3.1 70B", "T2",
        ["general"], 128000, "text", 1, "2024-07",
        "Meta 开源旗舰, 硅基流动常驻"
    ),
    (
        "llama-3.1-8b", "llama", "Llama 3.1 8B", "T3",
        ["general"], 128000, "text", 1, "2024-07",
        "Meta 轻量"
    ),
]


async def seed():
    await init_db()

    async with async_session_factory() as session:
        # 幂等: 先清再插 (跟 seed_model_cards.py 风格一致)
        await session.execute(delete(ModelRegistry))
        await session.commit()

        count = 0
        for (
            model_id, family, display_name, tier,
            capabilities, context_length, modality,
            is_open_source, release_date, notes,
        ) in MODEL_REGISTRY:
            # V4.6.4: notes 里含 "(deprecated)" 关键字 -> status="deprecated"
            # 避免变更 MODEL_REGISTRY tuple 结构 (向后兼容)
            status = "deprecated" if "(deprecated)" in (notes or "") else "active"
            entry = ModelRegistry(
                model_id=model_id,
                family=family,
                display_name=display_name,
                tier=tier,
                capabilities=json.dumps(capabilities, ensure_ascii=False),
                context_length=context_length,
                modality=modality,
                is_open_source=is_open_source,
                release_date=release_date,
                status=status,
                notes=notes,
            )
            session.add(entry)
            count += 1
        await session.commit()

        # 按 family + tier 分组展示
        rows = list((await session.execute(
            select(ModelRegistry).order_by(
                ModelRegistry.family, ModelRegistry.tier, ModelRegistry.model_id
            )
        )).scalars().all())
        print(f"\n=== Seeded {count} model registry entries ===")
        prev_family = None
        for r in rows:
            if r.family != prev_family:
                print(f"\n  [{r.family}]")
                prev_family = r.family
            caps = json.loads(r.capabilities) if r.capabilities else []
            ctx = f"{r.context_length // 1000}k" if r.context_length else "?"
            print(
                f"    {r.model_id:25s}  {r.tier}  ctx={ctx:5s}  "
                f"open={r.is_open_source}  caps={caps}"
            )

        # 统计
        print("\n=== 统计 ===")
        from collections import Counter
        tier_count = Counter(r.tier for r in rows)
        family_count = Counter(r.family for r in rows)
        print(f"  按 tier: {dict(tier_count)}")
        print(f"  按 family: {dict(family_count)}")


if __name__ == "__main__":
    asyncio.run(seed())
