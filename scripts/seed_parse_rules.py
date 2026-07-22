"""录入 ParseRule fallback 规则 (Phase 2B-2)

为每个 provider 录入一条 CSS 降级规则：
- 优先找 <main> 元素
- 没 main 找 <article>
- 都没就兜底 <body>
- confidence=0.5（标"低置信度"，比 AI 阈值 0.8 低）

用法：
    python scripts/seed_parse_rules.py
"""

import asyncio
import sys
from pathlib import Path

# 将项目根目录加入 sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from db.engine import async_session_factory
from db.init_db import init_db
from db import crud


# 默认规则（按 provider 配置）
DEFAULT_RULES = [
    {
        "provider": "deepseek",
        "css_selector": "main, article, body",
        "ai_strategy": "fallback: 优先 main/article，兜底 body",
        "confidence": 0.5,
        "url_pattern": "https://api.deepseek.com/*",
    },
    {
        "provider": "qwen",
        "css_selector": "main, article, body",
        "ai_strategy": "fallback: 优先 main/article，兜底 body",
        "confidence": 0.5,
        "url_pattern": "https://*.aliyun.com/*",
    },
    {
        "provider": "glm",
        "css_selector": "main, article, body",
        "ai_strategy": "fallback: 优先 main/article，兜底 body",
        "confidence": 0.5,
        "url_pattern": "https://open.bigmodel.cn/*",
    },
]


async def seed():
    await init_db()

    async with async_session_factory() as session:
        for rule_kwargs in DEFAULT_RULES:
            provider = rule_kwargs["provider"]
            rule = await crud.upsert_parse_rule(
                session,
                provider=provider,
                url_pattern=rule_kwargs.get("url_pattern"),
                css_selector=rule_kwargs.get("css_selector"),
                ai_strategy=rule_kwargs.get("ai_strategy"),
                confidence=rule_kwargs.get("confidence"),
                status="active",
            )
            print(
                f"[+] {provider}: css_selector={rule.css_selector!r}, "
                f"confidence={rule.confidence}"
            )

        await session.commit()
        print(f"\n[OK] {len(DEFAULT_RULES)} 条 ParseRule 已 seed")


if __name__ == "__main__":
    asyncio.run(seed())
