"""APScheduler 定时调度器

在 FastAPI 启动时注册定时任务，同进程异步运行。

改进历史：
- 2026-07-22: 加 run_source_now 手动触发 (Phase 2A-4, 借鉴 firecrawl 异步 job 模式)
"""

import asyncio
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
        # 用 provider + source_type 作为 job id 后缀
        # （source_id 默认 0，会冲突；同一 provider 下两个 source 通过 source_type 区分）
        job_id = f"crawl_{source.provider}_{source.source_type}"
        scheduler.add_job(
            _run_source_task,
            trigger=IntervalTrigger(seconds=source.poll_interval),
            args=[source],
            id=job_id,
            name=f"采集: {source.provider}/{source.source_type}",
            replace_existing=True,
        )
        logger.info(f"注册采集任务: {source.provider}/{source.source_type} (间隔 {source.poll_interval}s)")

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


async def run_source_now(provider: str) -> int:
    """立即触发指定 provider 的所有 source（不等待定时器）

    Args:
        provider: 厂商标识，如 'deepseek' / 'qwen' / 'glm'

    Returns:
        启动的 task 数量

    适用场景：
    - 改完解析逻辑想立刻验证
    - 刚加完新 source 不想等 15 分钟
    - 调试时反复触发

    借鉴：firecrawl 异步 job 模式（caller 不阻塞，task 在后台跑）
    """
    from intelligence.sources import get_source_by_provider

    sources = get_source_by_provider(provider)
    if not sources:
        logger.warning(f"手动触发失败：未知 provider '{provider}'")
        return 0

    count = 0
    for source in sources:
        # fire-and-forget：每个 source 一个独立 task
        asyncio.create_task(_run_source_task(source))
        count += 1

    logger.info(f"手动触发 {provider}: 启动 {count} 个 task")
    return count


async def _run_source_task(source):
    """执行单个采集源的任务

    Phase 2C-1: 接入 handle_fetch_error 告警
    - fetch 失败时调 handle_fetch_error（连续 3 次告警 + disable）
    - 外层 Exception 仍然只 log（开发者 bug 不应刷 Telegram）
    """
    try:
        from intelligence.fetcher import fetch_page_with_error
        from intelligence.differ import has_changed
        from intelligence.healer import handle_parse_result, handle_fetch_error

        # 1. 抓取页面（Phase 2C-1: 用 _with_error 版本拿错误原因）
        content, error = await fetch_page_with_error(source.url)
        if not content:
            logger.warning(f"采集失败: {source.provider} - {error}")
            # 调 handle_fetch_error：累加 error_count + 按规则告警
            await handle_fetch_error(source, error or "未知错误")
            return

        # 2. Diff 检测
        changed = await has_changed(source, content)
        if not changed:
            logger.debug(f"无变化，跳过: {source.provider}")
            return

        logger.info(f"检测到变化: {source.provider} -> 触发自愈流程")

        # 3. 自愈流程（AI 解析 → CSS 降级 → 告警）
        # Phase 2B-2: handle_parse_result 接收原始 content，自己跑 AI
        await handle_parse_result(content, source)

    except Exception as e:
        # 外层 Exception 是代码 bug，开发者排查用
        # 不发 Telegram 避免刷屏（设计选择）
        logger.error(f"采集任务异常: {source.provider} - {e}", exc_info=True)


async def _refresh_route_cache():
    """定时刷新路由缓存"""
    try:
        from gateway.router import route_engine
        await route_engine.refresh_cache()
    except Exception as e:
        logger.error(f"路由缓存刷新失败: {e}")
