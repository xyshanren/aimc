"""Seed ModelCard 表 (Phase 2D-4, 2026-07-24)

引用第三方公开 benchmark 分数（OpenCompass / LMSYS Arena），
让 dashboard / 路由决策有依据。

设计：
- 不自己做 benchmark（成本高、个人没必要）
- 引用第三方公开数据，标 source 即可
- **分数全部留 placeholder (0.0)** — 你从 OpenCompass / LMSYS 复制粘贴填

用法：
    1. 编辑本脚本，把每个 model 的 score 字段填上真实分数
    2. python scripts/seed_model_cards.py
    3. 重复运行会更新（用 model+benchmark 作为唯一键）

借鉴：OpenCompass (opencompass.org.cn) 公开榜单
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from db.engine import async_session_factory
from db.init_db import init_db
from db import crud
from db.models import ModelCard


# 模型评测卡 (Phase 2D-4, 2026-07-24 更新国内最新模型)
# 字段：
#   model, provider, benchmark, score (0-100), tier, source_url, notes
# score 默认 0.0 — 你从 OpenCompass / SuperCLUE 填真实分数
#
# 设计原则（用户偏好）：**只列国内模型**，不列 GPT/Claude/Gemini 等国外模型
#   - DeepSeek V4 (2026-04, 1M 上下文)
#   - Qwen 3.7/3.8 (2026-05/07, 多模态)
#   - GLM-5.2 (2026-06, 智谱最强开源, 1M 上下文, MIT)
#   - Kimi K3 (2026-07-16, 月之暗面, 2.8T 参数, 1M 上下文, 7-27 开源)
#   - MiniMax M2.7 / M3 / M3 Pro (2026-03/06/Q3)
MODEL_CARDS = [
    # ===== DeepSeek (V4 系列) =====
    # V4-Pro: 1.6T 总参, 49B 激活, 1M 上下文, 编程追平闭源
    ("deepseek-v4-pro", "deepseek", "OpenCompass 2026-Q2", 0.0, "code", "https://opencompass.org.cn/", "1.6T 参数, 49B 激活, 1M ctx"),
    # V4-Flash: 284B 总参, 13B 激活, 1M 上下文, 性价比路线
    ("deepseek-v4-flash", "deepseek", "OpenCompass 2026-Q2", 0.0, "general", "https://opencompass.org.cn/", "284B 参数, 13B 激活, 1M ctx, 极便宜"),

    # ===== Qwen 3.7/3.8 系列 =====
    # Qwen3.7-Max: 1.2T MoE, 激活 45B, 128K, 多模态
    ("qwen3.7-max", "qwen", "OpenCompass 2026-Q2", 0.0, "general", "https://opencompass.org.cn/", "1.2T 参数, 全域思考, 多模态"),
    # Qwen3.7-Plus: 35B 密集, 128K, 多模态
    ("qwen3.7-plus", "qwen", "OpenCompass 2026-Q2", 0.0, "chinese", "https://opencompass.org.cn/", "35B 密集, 中文场景首选"),
    # Qwen3.7-Flash: 轻量多模态
    ("qwen3.7-flash", "qwen", "OpenCompass 2026-Q2", 0.0, "general", "https://opencompass.org.cn/", "轻量快速"),
    # Qwen3.8-Max-Preview: 2.4T 参数, 2026-07 预览
    ("qwen3.8-max-preview", "qwen", "OpenCompass 2026-Q3", 0.0, "code", "https://opencompass.org.cn/", "2.4T 参数预览版"),

    # ===== GLM-5.2 (智谱最新旗舰) =====
    # GLM-5.2: 753B 总参, 40B 激活, 1M 上下文, MIT 开源
    # 2026-06-17 发布, 智谱迄今最强编程模型, Design Arena 第一
    ("glm-5.2", "glm", "OpenCompass 2026-Q2", 0.0, "code", "https://opencompass.org.cn/", "753B, 1M ctx, MIT 开源, Design Arena 第一"),

    # ===== Kimi K3 (月之暗面最新旗舰) =====
    # K3: 2.8T 参数, 1M 上下文, 原生多模态
    # 2026-07-16 发布, 全球参数最大开源模型, 7-27 完整权重开源
    # Arena WebDev 登顶 (1679 分, 超 Claude Fable 5)
    ("kimi-k3", "kimi", "OpenCompass 2026-Q3", 0.0, "code", "https://opencompass.org.cn/", "2.8T 参数, 1M ctx, Arena WebDev 第一, 7-27 开源"),

    # ===== MiniMax (M2.7 / M3 系列) =====
    # M2.7: 200K, 自我进化 (2026-03)
    ("MiniMax-M2.7", "MiniMax", "OpenCompass 2026-Q1", 0.0, "general", "https://opencompass.org.cn/", "自我进化, Agent 强"),
    # M2.7-highspeed: 极速版, TPS 100
    ("MiniMax-M2.7-highspeed", "MiniMax", "OpenCompass 2026-Q1", 0.0, "general", "https://opencompass.org.cn/", "TPS 100 极速版"),
    # M3: 196B 总参, 11B 激活, 1M 上下文, 原生多模态 (2026-06)
    ("MiniMax-M3", "MiniMax", "OpenCompass 2026-Q2", 0.0, "multimodal", "https://opencompass.org.cn/", "1M ctx, 原生多模态, SWE-Bench Pro 59%"),
]


async def seed():
    await init_db()

    async with async_session_factory() as session:
        # 清旧（演示用，生产保留历史）
        await session.execute(__import__('sqlalchemy').delete(ModelCard))
        await session.commit()

        count = 0
        for model, provider, benchmark, score, tier, source_url, notes in MODEL_CARDS:
            card = ModelCard(
                model=model, provider=provider, benchmark=benchmark,
                score=score, tier=tier, source_url=source_url, notes=notes,
            )
            session.add(card)
            count += 1
        await session.commit()

        # 显示
        cards = list((await session.execute(
            __import__('sqlalchemy').select(ModelCard).order_by(ModelCard.id)
        )).scalars().all())
        print(f"\n=== Seeded {count} model cards ===")
        for c in cards:
            print(f"  - {c.model:30s} ({c.provider:10s}) score={c.score:5.1f} tier={c.tier}")


if __name__ == "__main__":
    asyncio.run(seed())
