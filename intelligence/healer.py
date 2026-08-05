"""自愈循环模块

Phase 2B-2 改造后的流程：
1. AI 解析（异常向上抛）
2. 成功 + 置信度 ≥ 阈值 → 入库
3. 失败 OR 置信度 < 阈值 → CSS 降级（接 ParseRule 表）
4. CSS 降级成功 → 入库（标 parse_rule.confidence）
5. 全部失败 → 告警人工

借鉴 crawl4ai 多 extraction strategy fallback。

改进历史：
- 2026-07-24: Phase 2D-1 加 sanity_check + flagged 标黄
  - 解决 LLM 编造错误优惠/折扣的风险
  - 不阻止入库，标黄让 dashboard 醒目标记
"""

import json
import logging
import re

from config import settings
from schemas.intel import ParseResult

logger = logging.getLogger(__name__)

# Sanity check 阈值 (Phase 2D-1, 2026-07-24)
# 这些是经验值，不追求精确 — 宁可误标也不漏标（人工过滤比 AI 编造好）
_SANITY_MIN_CONFIDENCE = 0.6      # < 0.6 标黄（AI 自己都不确信）
_SANITY_MIN_TITLE_LEN = 5          # 标题太短大概率是解析失败
_SANITY_MAX_TITLE_LEN = 200        # 标题太长可能是 HTML 污染
_SANITY_MIN_DISCOUNT = 1.0         # "X 折" < 1 折 = 超过 99% 折扣（异常）
_SANITY_MAX_DISCOUNT = 9.5         # "X 折" > 9.5 折 = 折扣 < 5%（几乎没意义）

# Phase 3.1 (2026-07-29): 按 source_type 调整 sanity check 阈值
# model_list / benchmark / price_aggregator 不是促销信息
# AI 解析置信度天然较低（榜单变频繁/列表变频繁）
# 阈值放低到 0.4 避免大量误报标黄
_SOURCE_TYPE_SANITY_OVERRIDES = {
    "model_list": 0.4,
    "price_aggregator": 0.4,
    "benchmark": 0.4,
}


def _sanity_check(result: ParseResult, source_type: str = "web_page") -> tuple[bool, str | None]:
    """简单 sanity check 解析结果

    Phase 2D-1: 防止 LLM 编造看似合理的错误优惠入库
    Phase 3.1: 按 source_type 调整阈值 (model_list / benchmark / price_aggregator 放低)

    Args:
        result: AI 解析结果
        source_type: 采集源类型 (Phase 3.1 用于调阈值)

    Returns:
        (flagged, reason)
        - (False, None) 正常
        - (True, "low_confidence(0.45)") 异常（不阻止入库，标黄让人看）

    规则（按优先级）：
    1. confidence < 阈值 → low_confidence (Phase 3.1: model_list/benchmark/price_aggregator 用 0.4)
    2. title 太短/太长 → title_length
    3. "X 折" < 1 或 > 9.5 → abnormal_discount
    4. "免费/白嫖" 但 confidence < 0.8 → free_but_uncertain
    """
    # Phase 3.1: 按 source_type 选阈值
    min_confidence = _SOURCE_TYPE_SANITY_OVERRIDES.get(source_type, _SANITY_MIN_CONFIDENCE)
    if result.confidence < min_confidence:
        return True, f"low_confidence({result.confidence:.2f}, type={source_type})"

    title = result.title or ""
    if len(title) < _SANITY_MIN_TITLE_LEN:
        return True, f"title_too_short(len={len(title)})"
    if len(title) > _SANITY_MAX_TITLE_LEN:
        return True, f"title_too_long(len={len(title)})"

    desc = result.discount_desc or ""
    if desc:
        m = re.search(r"(\d+(?:\.\d+)?)\s*折", desc)
        if m:
            try:
                discount = float(m.group(1))
                if discount < _SANITY_MIN_DISCOUNT or discount > _SANITY_MAX_DISCOUNT:
                    return True, f"abnormal_discount({discount}折)"
            except ValueError:
                pass

        if result.confidence < 0.8 and ("免费" in desc or "白嫖" in desc):
            return True, f"free_but_uncertain({result.confidence:.2f})"

    return False, None


async def handle_parse_result(content: str, source):
    """自愈循环主入口

    Args:
        content: 页面 HTML（已通过 differ 检测有变化）
        source: 采集源对象

    流程图：
    ┌─────────────────┐
    │   AI 解析        │ try/except
    │  parse_content  │ ←──┐
    └────────┬────────┘    │
             │             │
        ┌────┴────┐        │
        │ 成功?   │        │ 异常
        ▼         ▼        │
    ┌──────┐  ┌──────┐     │
    │入库  │  │CSS降级│ ←──┘
    └──┬───┘  └──┬───┘
       │         │
       │    ┌────┴────┐
       │    │ 成功?   │
       │    ▼         ▼
       │ ┌──────┐  ┌──────┐
       │ │入库  │  │告警  │
       │ └──────┘  └──────┘
       │
       └─→ 全部结束
    """
    # === 1. AI 解析 ===
    ai_result: ParseResult | None = None
    try:
        from intelligence.parser import parse_content
        ai_result = await parse_content(content, source.provider, source.url)
        logger.debug(
            f"AI 解析完成: provider={source.provider}, type={source.source_type}, "
            f"is_promotion={ai_result.is_promotion}, "
            f"confidence={ai_result.confidence:.2f}"
        )
    except Exception as e:
        logger.warning(f"AI 解析异常: {source.provider} - {e}")
        ai_result = None

    # === 1.5 Phase 3.1: model_list / benchmark / price_aggregator 类型特殊处理 ===
    # 这些类型不是促销信息, 不应入 promotions 表 (会污染 dashboard)
    # 但当前是"半天最小可用版": 仍入 promotions, 加 source_type 标记让 3.3 消费者认出来
    # 后续可以加 model_registry_candidates / benchmark_snapshots 表分流 (但需要 schema 改动)

    # === 2. AI 成功 + 置信度足够 → 入库 ===
    if ai_result and ai_result.is_promotion and ai_result.confidence >= settings.parse_confidence_threshold:
        await _save_promotion(ai_result, source)
        logger.info(
            f"优惠入库(AI): [{ai_result.provider}] {ai_result.title} "
            f"(置信度: {ai_result.confidence:.2f})"
        )
        return

    # === 3. 触发降级：AI 失败 OR AI 成功但低置信度 OR AI 判断非优惠 ===
    # （注：即使 AI 判断"非优惠"，CSS 降级也可能捞到手动漏掉的活动）
    css_result = await _try_css_fallback(content, source)
    if css_result:
        await _save_promotion(css_result, source)
        logger.info(
            f"优惠入库(CSS 降级): [{css_result.provider}] "
            f"selector={css_result.summary!r}"
        )
        return

    # === 4. 全部失败 → 告警 ===
    if ai_result and ai_result.is_promotion:
        # AI 解析出"是优惠"但 CSS 降级也没救 → 人工确认
        await _alert_low_confidence(ai_result, source)
    else:
        # AI 失败 / AI 判断非优惠 / CSS 降级也失败 → 静默
        logger.debug(
            f"非优惠或全部失败，跳过: {source.provider} "
            f"(ai={'failed' if ai_result is None else 'no_promotion'})"
        )


async def _try_css_fallback(content: str, source) -> ParseResult | None:
    """尝试 CSS 降级

    Returns:
        ParseResult 成功提取 / None 没有可用 rule 或提取失败
    """
    try:
        from db.engine import async_session_factory
        from db import crud
        from intelligence.extractor import extract_by_css

        async with async_session_factory() as session:
            rule = await crud.get_active_parse_rule(session, source.provider)

        if not rule:
            logger.debug(f"无 {source.provider} 的 CSS 降级规则")
            return None

        logger.info(
            f"尝试 CSS 降级: {source.provider} (rule.confidence={rule.confidence:.2f}, "
            f"selector={rule.css_selector!r})"
        )
        return extract_by_css(
            html=content,
            css_selector=rule.css_selector,
            confidence=rule.confidence or 0.5,
            provider=source.provider,
            url=source.url,
        )
    except Exception as e:
        logger.error(f"CSS 降级异常: {e}")
        return None


async def _save_promotion(result: ParseResult, source):
    """将解析结果存入优惠活动表

    Phase 2D-1: 加 sanity_check 标黄
    - 标黄不阻止入库（你之前说倾向后者 — 不丢数据，只是醒目标记）
    - 标黄原因写到 flagged_reason

    Phase 3.1: 接收 source_type
    - sanity check 阈值按 type 调整 (model_list/benchmark/price_aggregator 放低)
    - source_type 写进 raw_data JSON, 让 3.3 消费者认出来
    """
    try:
        from db.engine import async_session_factory
        from db import crud

        # sanity check（先于入库，但不影响入库）— Phase 3.1: 传 source_type
        flagged, flagged_reason = _sanity_check(result, source.source_type)

        # Phase 3.1: raw_data 附加 source_type, 便于后续 3.3 消费
        raw_data_dict = result.model_dump()
        raw_data_dict["_source_type"] = source.source_type

        async with async_session_factory() as session:
            await crud.create_promotion(
                session,
                provider=result.provider or source.provider,
                title=result.title or "未知活动",
                content=result.content,
                url=result.url,
                discount_desc=result.discount_desc,
                start_date=result.start_date,
                end_date=result.end_date,
                status="active",
                confidence=result.confidence,
                raw_data=json.dumps(raw_data_dict, ensure_ascii=False),
                flagged=1 if flagged else 0,
                flagged_reason=flagged_reason,
            )
            await session.commit()

        if flagged:
            logger.warning(
                f"⚠️  优惠入库(标黄): [{result.provider or source.provider}] "
                f"{result.title!r} reason={flagged_reason}"
            )
    except Exception as e:
        logger.error(f"优惠入库失败: {e}")


async def _alert_low_confidence(result: ParseResult, source):
    """低置信度告警"""
    from intelligence.alerter import send_alert

    message = (
        f"[低置信度] {source.provider} 发现疑似优惠\n"
        f"标题: {result.title}\n"
        f"置信度: {result.confidence:.2f}\n"
        f"摘要: {result.summary}\n"
        f"链接: {result.url}\n"
        f"请人工确认是否入库。"
    )
    await send_alert(message)


async def handle_fetch_error(source, error: str):
    """处理采集失败

    Phase 2C-1: 升级告警策略
    - 第 1 次失败：立即告警（不要延迟发现故障）
    - 第 2 次失败：log only（避免刷屏）
    - 第 3 次失败：告警 + 自动 disable source
    - 第 4+ 次：log only（已 disabled）

    Args:
        source: 采集源对象（in-memory error_count 会被累加并同步到 DB）
        error: 错误描述（来自 fetch_page_with_error 的 reason）
    """
    try:
        from db.engine import async_session_factory
        from db import crud
        from intelligence.alerter import send_alert

        async with async_session_factory() as session:
            # 增加错误计数（in-memory + DB）
            source.error_count = (source.error_count or 0) + 1

            # Phase 2C-2: 用 (provider, url) 替代 source_id
            db_source = await crud.get_intel_source_by_provider_url(
                session, source.provider, source.url
            )
            if db_source:
                db_source.error_count = source.error_count
                if source.error_count >= 3:
                    db_source.status = "error"
                await session.commit()
            else:
                # DB 没对应行（首次失败）→ upsert 一条
                await crud.upsert_intel_source(
                    session,
                    provider=source.provider,
                    url=source.url,
                    source_type=getattr(source, "source_type", "web_page"),
                    poll_interval=getattr(source, "poll_interval", 900),
                    status="error" if source.error_count >= 3 else "active",
                    error_count=source.error_count,
                )
                await session.commit()

            if source.error_count == 1:
                # 首次失败：立即告警
                await send_alert(
                    f"[采集异常] {source.provider} 首次失败\n"
                    f"URL: {source.url}\n"
                    f"错误: {error}\n"
                    f"error_count={source.error_count}/3"
                )
            elif source.error_count >= 3:
                # 连续 3 次失败：升级告警 + 自动禁用
                await send_alert(
                    f"[采集异常] {source.provider} 连续失败 {source.error_count} 次\n"
                    f"URL: {source.url}\n"
                    f"错误: {error}\n"
                    f"已自动禁用，请人工检查。"
                )

    except Exception as e:
        logger.error(f"处理采集错误失败: {e}")
