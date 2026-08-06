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
import re
from typing import Optional

from openai import AsyncOpenAI

from config import settings
from intelligence.cleaner import clean_html
from schemas.intel import ModelIntelItem, ParseResult

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


SYSTEM_PROMPT = """你是一个 AI 大模型情报分析助手。

你将收到一段已经过清洗的 Markdown 页面内容，以及采集源类型 source_type。
请同时识别优惠信息和模型元信息，并返回严格 JSON：
{
  "is_promotion": false,
  "confidence": 0.0,
  "title": null,
  "provider": "厂商标识",
  "discount_desc": null,
  "content": "摘要",
  "start_date": null,
  "end_date": null,
  "summary": "简短中文总结",
  "intelligence_kind": "promotion|model_metadata|benchmark|pricing|other",
  "models": [
    {
      "model_id": "页面原始模型 ID",
      "family": "deepseek|qwen|glm|kimi|doubao|other",
      "display_name": "展示名称",
      "inferred_tier": "T0|T1|T2|T3|T4|null",
      "capabilities": ["general|reasoning|code|math|vision|tool_use|long_context"],
      "context_length": 32768,
      "modality": "text|vision|multimodal",
      "is_open_source": true,
      "status": "active|preview|deprecated",
      "confidence": 0.0,
      "tier_reason": "推断依据"
    }
  ]
}

规则：
- source_type=model_list 时重点提取 models，即使没有优惠也必须返回模型列表。
- tier 表示综合能力（参考 architecture.md §4.2）：T0 旗舰前沿（如 DeepSeek-V4-Pro、Qwen3-235B），T1 强力（如 DeepSeek-V3），T2 均衡（如 Qwen-Plus），T3 轻量（如 Qwen2.5-7B），T4 微型/嵌入（如 Qwen2.5-1.5B）。
- 不确定的 tier 设 null，禁止无依据猜测；每个模型 confidence 独立评分。
- capabilities 只能使用给定标签；无法确定的字段设为 null 或空数组。
- confidence 必须在 0.0-1.0；日期统一 YYYY-MM-DD。
"""

_ALLOWED_TIERS = {"T0", "T1", "T2", "T3", "T4"}
_ALLOWED_CAPABILITIES = {
    "general", "reasoning", "code", "math", "vision", "tool_use", "long_context",
}


def confidence_level(confidence: float) -> str:
    """将置信度分为 low/medium/high，供 Phase 3.3 安全阀消费。"""
    if confidence >= 0.8:
        return "high"
    if confidence >= 0.6:
        return "medium"
    return "low"


def infer_tier(model_id: str, explicit_tier: Optional[str] = None) -> tuple[Optional[str], str]:
    """优先采用 AI 明确 tier；否则仅按模型参数规模做保守推断。"""
    if explicit_tier in _ALLOWED_TIERS:
        return explicit_tier, "AI 根据页面能力描述推断"

    match = re.search(r"(?:^|[-_/])(\d+(?:\.\d+)?)\s*[bB](?:$|[-_/])", model_id)
    if not match:
        return None, "缺少可靠参数规模或能力依据"

    size = float(match.group(1))
    # 语义以 architecture.md §4.2 为准：T0=旗舰/参数最大，T4=微型/参数最小。
    if size >= 200:
        return "T0", f"参数规模约 {size:g}B (旗舰档)"
    if size >= 70:
        return "T1", f"参数规模约 {size:g}B (强力档)"
    if size >= 20:
        return "T2", f"参数规模约 {size:g}B (均衡档)"
    if size >= 7:
        return "T3", f"参数规模约 {size:g}B (轻量档)"
    return "T4", f"参数规模约 {size:g}B (微型/嵌入档)"


def normalize_model_item(data: dict) -> ModelIntelItem:
    """清洗 LLM 模型元信息，限制枚举并补充分级字段。"""
    model_id = str(data.get("model_id") or "").strip()
    if not model_id:
        raise ValueError("模型元信息缺少 model_id")

    raw_confidence = data.get("confidence", 0.0)
    try:
        confidence = max(0.0, min(1.0, float(raw_confidence)))
    except (TypeError, ValueError):
        confidence = 0.0

    tier, fallback_reason = infer_tier(model_id, data.get("inferred_tier"))
    raw_caps = data.get("capabilities") or []
    capabilities = sorted({
        str(cap).strip().lower() for cap in raw_caps
        if str(cap).strip().lower() in _ALLOWED_CAPABILITIES
    })
    context_length = data.get("context_length")
    try:
        context_length = int(context_length) if context_length is not None else None
        if context_length is not None and context_length <= 0:
            context_length = None
    except (TypeError, ValueError):
        context_length = None

    modality = data.get("modality")
    if modality not in {"text", "vision", "multimodal"}:
        modality = "text"
    status = data.get("status")
    if status not in {"active", "preview", "deprecated"}:
        status = "active"

    return ModelIntelItem(
        model_id=model_id,
        family=data.get("family"),
        display_name=data.get("display_name"),
        inferred_tier=tier,
        capabilities=capabilities,
        context_length=context_length,
        modality=modality,
        is_open_source=data.get("is_open_source"),
        status=status,
        confidence=confidence,
        confidence_level=confidence_level(confidence),
        tier_reason=data.get("tier_reason") or fallback_reason,
    )


async def parse_content(
    content: str,
    provider: str,
    url: str,
    source_type: str = "web_page",
) -> ParseResult:
    """使用 AI 解析页面内容

    Args:
        content: 页面 HTML（来自 fetcher 的 page.content()）
        provider: 厂商标识
        url: 来源 URL
        source_type: 采集源类型，决定优惠或模型元信息提取重点

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
采集源类型：{source_type}
清洗后的 Markdown 内容：
---
{cleaned_md}
---

请按 source_type 分析优惠、价格、Benchmark 或模型元信息，返回 JSON。"""

    response = await client.chat.completions.create(
        model=settings.parse_model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        response_format={"type": "json_object"},
        temperature=0.1,
        max_tokens=1200,
    )

    result_text = response.choices[0].message.content
    data = json.loads(result_text)  # JSONDecodeError 向上抛

    models = []
    for item in data.get("models") or []:
        if not isinstance(item, dict):
            continue
        try:
            models.append(normalize_model_item(item))
        except ValueError as exc:
            logger.warning("跳过无效模型元信息: %s", exc)

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
        source_type=source_type,
        intelligence_kind=data.get("intelligence_kind", "promotion"),
        models=models,
    )
