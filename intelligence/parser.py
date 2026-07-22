"""AI 解析引擎

使用 LLM（gpt-4o-mini）对抓取的页面内容进行结构化提取。
成本极低，适合高频调用。

改进历史：
- 2026-07-22: 接 HTML 净化器 (Phase 2B-1)
  - 之前：直接截断 3000 字符 HTML（含 nav/footer/script 噪音）
  - 现在：cleaner.py 净化成 Markdown 后截断 1500（等价信息 50% token）
"""

import json
import logging
from typing import Optional

from openai import AsyncOpenAI

from config import settings
from intelligence.cleaner import clean_html
from schemas.intel import ParseResult

logger = logging.getLogger(__name__)

# OpenAI 客户端（延迟初始化）
_client: Optional[AsyncOpenAI] = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
        )
    return _client


SYSTEM_PROMPT = """你是一个 AI 大模型优惠情报分析助手。

你将收到一段已经过清洗的 Markdown 页面内容（去除了导航/页脚/广告/脚本等噪音）。
请分析是否包含 AI 模型相关的优惠/活动/价格变动信息。

返回严格的 JSON 格式：
{
    "is_promotion": true/false,
    "confidence": 0.0-1.0,
    "title": "活动标题（如有）",
    "provider": "厂商标识（deepseek/qwen/glm/doubao/hunyuan/other）",
    "discount_desc": "优惠描述，如'5折'、'免费100万token'",
    "content": "活动内容摘要（50字以内）",
    "start_date": "开始日期 YYYY-MM-DD（如有）",
    "end_date": "截止日期 YYYY-MM-DD（如有）",
    "summary": "简短中文总结（20字以内）"
}

注意：
- 如果不是优惠/活动信息，is_promotion 设为 false，confidence 设为判断置信度
- 日期格式统一为 YYYY-MM-DD
- 无法确定的字段设为 null
- 输入是 Markdown 格式（含 # 标题、`代码`、[链接](url)），直接按语义理解即可
"""


async def parse_content(content: str, provider: str, url: str) -> ParseResult:
    """使用 AI 解析页面内容

    Args:
        content: 页面 HTML（来自 fetcher 的 page.content()）
        provider: 厂商标识
        url: 来源 URL

    Returns:
        ParseResult 结构化解析结果

    Raises:
        Exception: 任何 AI 调用失败（OpenAIError / JSONDecodeError / 网络超时等）
        由 healer.py 负责 try/except，决定走 CSS 降级 / 告警

    改进历史：
    - 2026-07-22: 移除内部 try/except，让异常向上抛给 healer
      之前是"AI 失败就返回 is_promotion=False"——这样 healer 看不到失败
      现在 healer 自己 try/except，能区分"AI 失败"和"AI 成功但不是优惠"
    """
    client = _get_client()  # 缺 key 时这里抛 OpenAIError

    # 1. 净化 HTML → 干净 Markdown
    cleaned_md = clean_html(content, max_chars=1500)

    user_prompt = f"""页面来源：{provider} ({url})
清洗后的 Markdown 内容：
---
{cleaned_md}
---

请分析此页面是否包含 AI 模型优惠/活动/价格变动信息，返回 JSON。"""

    response = await client.chat.completions.create(
        model=settings.parse_model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        response_format={"type": "json_object"},
        temperature=0.1,
        max_tokens=500,
    )

    result_text = response.choices[0].message.content
    data = json.loads(result_text)  # JSONDecodeError 向上抛

    return ParseResult(
        is_promotion=data.get("is_promotion", False),
        confidence=data.get("confidence", 0.0),
        title=data.get("title"),
        provider=data.get("provider", provider),
        discount_desc=data.get("discount_desc"),
        content=data.get("content"),
        start_date=data.get("start_date"),
        end_date=data.get("end_date"),
        url=url,
        summary=data.get("summary"),
    )
