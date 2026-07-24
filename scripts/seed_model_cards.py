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


# 模型评测卡 (Phase 2D-4)
# 字段：
#   model, provider, benchmark, score (0-100), tier, source_url, notes
# score 默认 0.0 — 你填真实分数
MODEL_CARDS = [
    # DeepSeek
    ("deepseek-chat", "deepseek", "OpenCompass 2024-Q3", 0.0, "general", "https://opencompass.org.cn/", "snapshot 2024-09"),
    ("deepseek-reasoner", "deepseek", "OpenCompass 2024-Q3", 0.0, "code", "https://opencompass.org.cn/", "强推理/代码"),
    # Qwen
    ("qwen-max", "qwen", "OpenCompass 2024-Q3", 0.0, "general", "https://opencompass.org.cn/", ""),
    ("qwen-plus", "qwen", "OpenCompass 2024-Q3", 0.0, "chinese", "https://opencompass.org.cn/", "中文场景"),
    # GLM
    ("glm-4-plus", "glm", "OpenCompass 2024-Q3", 0.0, "general", "https://opencompass.org.cn/", ""),
    ("glm-4-flash", "glm", "OpenCompass 2024-Q3", 0.0, "chinese", "https://opencompass.org.cn/", "快速 + 便宜"),
    # MiniMax 代理的 OpenAI 模型 (示例)
    ("gpt-4o-mini", "minimax", "OpenCompass 2024-Q3", 0.0, "general", "https://opencompass.org.cn/", "MiniMax OpenAI-compatible 转发"),
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
