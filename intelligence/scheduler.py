"""APScheduler 定时调度器

在 FastAPI 启动时注册定时任务，同进程异步运行。
"""

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from config import settings

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


async def start_scheduler():
    """启动情报采集调度器"""
    from intelligence.sources import get_all_sources

    sources = get_all_sources()

    for source in sources:
        scheduler.add_job(
            _run_source_task,
            trigger=IntervalTrigger(seconds=source.poll_interval),
            args=[source],
            id=f"crawl_{source.provider}_{source.source_id}",
            name=f"采集: {source.provider}",
            replace_existing=True,
        )
        logger.info(f"注册采集任务: {source.provider} (间隔 {source.poll_interval}s)")

    # 路由缓存定时刷新
    scheduler.add_job(
        _refresh_route_cache,
        trigger=IntervalTrigger(seconds=settings.route_refresh_interval),
        id="refresh_route_cache",
        name="刷新路由缓存",
        replace_existing=True,
    )

    scheduler.start()
    logger.info(f"调度器已启动，共 {len(sources) + 1} 个定时任务")


async def stop_scheduler():
    """停止调度器"""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("调度器已停止")


async def _run_source_task(source):
    """执行单个采集源的任务"""
    try:
        from intelligence.fetcher import fetch_page
        from intelligence.differ import has_changed
        from intelligence.parser import parse_content
        from intelligence.healer import handle_parse_result

        # 1. 抓取页面
        content = await fetch_page(source.url)
        if not content:
            logger.warning(f"采集失败（空内容）: {source.provider}")
            return

        # 2. Diff 检测
        changed = await has_changed(source, content)
        if not changed:
            logger.debug(f"无变化，跳过: {source.provider}")
            return

        logger.info(f"检测到变化: {source.provider} -> 触发 AI 解析")

        # 3. AI 解析
        result = await parse_content(content, source.provider, source.url)

        # 4. 处理解析结果（入库 / 自愈 / 告警）
        await handle_parse_result(result, source)

    except Exception as e:
        logger.error(f"采集任务异常: {source.provider} - {e}", exc_info=True)


async def _refresh_route_cache():
    """定时刷新路由缓存"""
    try:
        from gateway.router import route_engine
        await route_engine.refresh_cache()
    except Exception as e:
        logger.error(f"路由缓存刷新失败: {e}")
