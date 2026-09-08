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
from schemas.intel import ModelIntelItem, ModelPricingItem, ParseResult

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
      "tier_reason": "推断依据",
      "pricing": [
        {
          "type": "normal|cache_hit|free|peak|offpeak",
          "input_price": 6.0,
          "output_price": 12.0,
          "time_range": "全天"
        }
      ]
    }
  ]
}

规则：
- source_type=model_list 时重点提取 models，即使没有优惠也必须返回模型列表。
- source_type=price_aggregator 时重点提取 pricing + promotions:
  1. 含 model × 价格表 → intelligence_kind="pricing", 每个 model 必须返回 pricing 列表
  2. 仅含订阅优惠/新品发布 → intelligence_kind="promotion"
  3. 同时含两者 → 优先 pricing (价格确定性更高)
- 价格类型识别:
  - 表格列 "缓存命中" 或 "cache hit" → type="cache_hit", input_price/output_price 通常为 0
  - "免费" / "限时免费" / "Free" → type="free", input_price=0, output_price=0
  - 常规按时段计费 → type="normal", time_range="全天" 或具体时段如 "00:00-08:00"
  - 峰谷计费 (e.g. DeepSeek "忙时"/"闲时") → 高峰 type="peak", time_range="工作日09:00-12:00" (按实际时段); 低谷 type="offpeak", time_range="非高峰全天"
    注意: 如果页面只列出高峰时段 (如 "工作日 9:00-12:00, 14:00-18:00 忙时"), 低谷用 type="offpeak", time_range="非高峰全天" 兜底
- 不确定的 tier 设 null，禁止无依据猜测；每个模型 confidence 独立评分。
- capabilities 只能使用给定标签；无法确定的字段设为 null 或空数组。
- confidence 必须在 0.0-1.0；日期统一 YYYY-MM-DD。
- 不要返回 provider 字段；厂商标识由采集源配置提供，与页面内容无关。
"""

_ALLOWED_TIERS = {"T0", "T1", "T2", "T3", "T4"}
_ALLOWED_CAPABILITIES = {
    "general", "reasoning", "code", "math", "vision", "tool_use", "long_context",
}
# Phase 4 v4.3 (2026-08-13): 价格类型白名单
_ALLOWED_PRICE_TYPES = {"normal", "cache_hit", "free", "peak", "offpeak"}


# === Thinking model 响应预处理 (V4.6.4, 2026-08-13) ===
# 背景: thinking model (MiniMax M2.7 / M2.7-highspeed / M3 / DeepSeek-R1 /
#       Qwen3-Thinking 等) 默认开启 reasoning, response.choices[0].message.content
#       会输出<think>块 + markdown fence 包裹的 JSON.
# 老代码 json.loads(result_text) 报 JSONDecodeError, 导致整条采集降级到 CSS 抽取.
# 修复: 剥掉 thinking 块 + 剥掉 markdown fence, 只留纯 JSON.

import re as _re_parser_helper
_THINK_BLOCK_RE = _re_parser_helper.compile(
    r"<think>.*?</think>",
    _re_parser_helper.DOTALL,
)
# fence: ```json { ... } ```  或  ``` { ... } ```  (语言名可选)
# 重要: 用 lazy match 先抓到 fence, 然后用 _balanced_braces 函数抓实际的 JSON
# (上面的 brace_pair 正则只支持 1 层嵌套, 真实 AIMC models 数组是 2 层嵌套)
_FENCE_RE = _re_parser_helper.compile(
    r"```(?:json)?\s*(\{)",
    _re_parser_helper.DOTALL,
)


def _balanced_braces(text: str, start: int = 0) -> tuple[int, int] | None:
    """从 start 开始找下一个 balanced { ... } 范围

    Args:
        text: 输入字符串
        start: 从该索引开始扫描

    Returns:
        (start_idx, end_idx_exclusive) 或 None (没找到)

    支持任意深度嵌套 (手写 stack, 避免 regex recursion 的 re 库依赖)
    """
    n = len(text)
    i = start
    while i < n:
        if text[i] == "{":
            depth = 1
            j = i + 1
            in_str = False
            escape_next = False
            while j < n and depth > 0:
                ch = text[j]
                if escape_next:
                    escape_next = False
                    j += 1
                    continue
                if in_str:
                    if ch == "\\":
                        escape_next = True
                    elif ch == '"':
                        in_str = False
                else:
                    if ch == '"':
                        in_str = True
                    elif ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                j += 1
            if depth == 0:
                return (i, j)
            return None
        i += 1
    return None


def _extract_json_from_response(text):
    """从 LLM response.content 抽出纯 JSON 字符串 (V4.6.4+)

    背景: thinking model 默认在 content 里输出<think>块 + markdown fence,
    老代码 json.loads(result_text) 会报 JSONDecodeError.

    设计: 按优先级试多种方式剥出 JSON, 都失败则原样返回(由调用者处理).
    1. thinking 块全剥掉 (re.sub)
    2. markdown fence ```...``` 包裹 (用 _balanced_braces 抓任意深度嵌套)
    3. 字符串中查找 balanced { ... } (任意深度, 用 stack-based parser)
    4. 前三步都不行: 原样返回 (老代码逻辑, 让 json.loads 自然抛错)

    Args:
        text: LLM 返回的 content

    Returns:
        剥出后的 JSON 字符串 (不保证一定合法, 由调用者 json.loads 验证)

    Raises:
        TypeError: text is None
    """
    if text is None:
        raise TypeError("response content is None")

    raw = text.strip()

    # Step 1: 剥掉 thinking 块
    cleaned = _THINK_BLOCK_RE.sub("", raw).strip()

    # Step 2: 剥掉 markdown fence 包裹 — fence 内的 { ... } 用 balanced_braces 提取
    fence_match = _FENCE_RE.search(cleaned)
    if fence_match:
        start = fence_match.start(1)
        brace_range = _balanced_braces(cleaned, start)
        if brace_range:
            candidate = cleaned[brace_range[0]:brace_range[1]]
            try:
                json.loads(candidate)
                return candidate
            except json.JSONDecodeError:
                pass  # 不是合法 JSON, 试下一步

    # Step 3: 任意深度 balanced { ... } 扫描
    pos = 0
    while True:
        brace_range = _balanced_braces(cleaned, pos)
        if not brace_range:
            break
        candidate = cleaned[brace_range[0]:brace_range[1]]
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            pass  # 试下一个 brace match
        pos = brace_range[1]

    # Step 4: 起始未能剥出: 原样返回 (老代码逻辑)
    return cleaned


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

    # Phase 4 v4.3 (2026-08-13): 清洗 pricing 字段
    raw_pricing = data.get("pricing") or []
    pricing_list: list[ModelPricingItem] = []
    for item in raw_pricing:
        if not isinstance(item, dict):
            continue
        price_type = str(item.get("type") or "normal").strip().lower()
        if price_type not in _ALLOWED_PRICE_TYPES:
            price_type = "normal"
        # 价格转 float, 失败 None
        def _to_float(v):
            if v is None:
                return None
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
        pricing_list.append(ModelPricingItem(
            type=price_type,
            input_price=_to_float(item.get("input_price")),
            output_price=_to_float(item.get("output_price")),
            time_range=str(item.get("time_range") or "全天").strip() or "全天",
        ))

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
        pricing=pricing_list,
        # V4.8.6: 透传采集来源渠道
        source_provider=data.get("source_provider"),
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
    # V4.6.4: thinking model 预处理 (MiniMax / DeepSeek-R1 / Qwen3-Thinking 等)
    # 兼容<think>块 + markdown fence 的输出格式
    try:
        json_text = _extract_json_from_response(result_text)
        data = json.loads(json_text)
    except json.JSONDecodeError as exc:
        # 输出脱敏的 response_text 前 200 字符 + provider/model 用于排查
        # V4.6.4: 之前抛 Exception 让 healer 收到, 看不到 response content
        sanitized = (result_text or "")[:200].replace("\n", "\\n")
        logger.warning(
            f"[parser] JSON 解析失败: provider={provider} model={settings.parse_model} "
            f"err={exc.msg[:50]} response[:200]={sanitized!r}"
        )
        raise

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
        # V6.3.2: provider 一律取采集源配置的规范值, 不信 AI 自由文本
        # 背景: AI 会返回 "Kimi (Moonshot AI)" / "SenseNova 商汤" 等变体,
        # 覆盖 source.provider 后入库, 导致看板按厂商聚合出现 17 种写法
        provider=provider,
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
