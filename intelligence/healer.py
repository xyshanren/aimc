"""自愈循环模块

根据 AI 解析结果的置信度决定处理方式：
- 置信度 > 阈值：自动入库
- 置信度 < 阈值：告警人工介入
- 解析失败：记录错误，触发告警
"""

import json
import logging

from config import settings
from schemas.intel import ParseResult

logger = logging.getLogger(__name__)


async def handle_parse_result(result: ParseResult, source):
    """处理 AI 解析结果

    Args:
        result: AI 解析结果
        source: 采集源对象
    """
    # 非优惠信息，跳过
    if not result.is_promotion:
        logger.debug(f"非优惠信息，跳过: {source.provider} - {result.summary}")
        return

    # 置信度判断
    if result.confidence >= settings.parse_confidence_threshold:
        # 高置信度：自动入库
        await _save_promotion(result, source)
        logger.info(
            f"优惠入库: [{result.provider}] {result.title} "
            f"(置信度: {result.confidence:.2f})"
        )
    else:
        # 低置信度：告警人工确认
        logger.warning(
            f"低置信度解析: [{result.provider}] {result.title} "
            f"(置信度: {result.confidence:.2f} < {settings.parse_confidence_threshold})"
        )
        await _alert_low_confidence(result, source)


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

    连续失败超过 3 次触发告警。
    """
    try:
        from db.engine import async_session_factory
        from db import crud

        async with async_session_factory() as session:
            # 增加错误计数
            source.error_count = (source.error_count or 0) + 1
            await crud.update_intel_source(
                session,
                source.source_id,
                error_count=source.error_count,
            )

            # 连续失败超过 3 次，标记为 error 并告警
            if source.error_count >= 3:
                await crud.update_intel_source(session, source.source_id, status="error")
                from intelligence.alerter import send_alert
                await send_alert(
                    f"[采集异常] {source.provider} 连续失败 {source.error_count} 次\n"
                    f"URL: {source.url}\n"
                    f"错误: {error}\n"
                    f"已自动禁用，请人工检查。"
                )

            await session.commit()
    except Exception as e:
        logger.error(f"处理采集错误失败: {e}")
