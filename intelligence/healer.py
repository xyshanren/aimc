"""自愈循环模块

Phase 2B-2 改造后的流程：
1. AI 解析（异常向上抛）
2. 成功 + 置信度 ≥ 阈值 → 入库
3. 失败 OR 置信度 < 阈值 → CSS 降级（接 ParseRule 表）
4. CSS 降级成功 → 入库（标 parse_rule.confidence）
5. 全部失败 → 告警人工

借鉴 crawl4ai 多 extraction strategy fallback。
"""

import json
import logging

from config import settings
from schemas.intel import ParseResult

logger = logging.getLogger(__name__)


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
            f"AI 解析完成: provider={source.provider}, "
            f"is_promotion={ai_result.is_promotion}, "
            f"confidence={ai_result.confidence:.2f}"
        )
    except Exception as e:
        logger.warning(f"AI 解析异常: {source.provider} - {e}")
        ai_result = None

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
    """将解析结果存入优惠活动表"""
    try:
        from db.engine import async_session_factory
        from db import crud

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
                raw_data=json.dumps(result.model_dump(), ensure_ascii=False),
            )
            await session.commit()
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
            await crud.update_intel_source(
                session,
                source.source_id,
                error_count=source.error_count,
            )

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
                await crud.update_intel_source(session, source.source_id, status="error")
                await send_alert(
                    f"[采集异常] {source.provider} 连续失败 {source.error_count} 次\n"
                    f"URL: {source.url}\n"
                    f"错误: {error}\n"
                    f"已自动禁用，请人工检查。"
                )

            await session.commit()
    except Exception as e:
        logger.error(f"处理采集错误失败: {e}")
