"""APScheduler 定时调度器

在 FastAPI 启动时注册定时任务，同进程异步运行。

改进历史：
- 2026-07-22: 加 run_source_now 手动触发 (Phase 2A-4, 借鉴 firecrawl 异步 job 模式)
- 2026-07-22: APScheduler 改用 SQLAlchemyJobStore (Phase 2C-2)
  - 之前: MemoryJobStore（重启丢 jobs）
  - 现在: SQLite 持久化（重启任务不丢，跟 firecrawl BullMQ 思想类似）
"""

import asyncio
import logging
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from config import settings

logger = logging.getLogger(__name__)


def _build_scheduler() -> AsyncIOScheduler:
    """构建 scheduler（Phase 2C-2: 用 SQLAlchemyJobStore 持久化 jobs）

    重要：用 `url=` 而不是 `engine=`，因为：
    - `url=` 时 SQLAlchemyJobStore 自动建表 + 自动 commit
    - `engine=` 时不自动建表，需要手动 Base.metadata.create_all()
    """
    from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

    # 从 settings.db_path 推导 sync sqlite URL
    # settings.db_path = './data/aimc.db' → sqlite:///./data/aimc.db
    db_path = Path(settings.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    sync_db_url = f"sqlite:///{db_path.as_posix()}"

    jobstore = SQLAlchemyJobStore(
        url=sync_db_url,
        tablename="apscheduler_jobs",
    )

    return AsyncIOScheduler(jobstores={"default": jobstore})


# 模块级 scheduler 单例（持久化模式）
scheduler = _build_scheduler()


async def start_scheduler():
    """启动情报采集调度器

    Phase 2C-2: jobs 持久化到 SQLite
    - 启动时自动 reload 上次未执行的 jobs
    - 重启服务后任务不丢（如果服务挂了，apscheduler_jobs 表里 next_run_time 仍存着）
    """
    from intelligence.sources import get_all_sources

    sources = get_all_sources()

    for source in sources:
        # 用 provider + source_type 作为 job id 后缀
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

    # API-Sec-3 (2026-07-29): 每天扫 1 次 key 轮换 (30 天阈值)
    scheduler.add_job(
        _check_key_rotation_task,
        trigger=IntervalTrigger(hours=24),
        id="check_key_rotation",
        name="key 轮换检查",
        replace_existing=True,
    )

    scheduler.start()
    logger.info(
        f"调度器已启动 (持久化模式), 共 {len(sources) + 1} 个定时任务, "
        f"jobstore={settings.db_path}"
    )


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

    Phase 2D-2: 加 crawl_enabled 总开关
    - 试用阶段关掉 → _run_source_task 第一行 short-circuit
    - 避免刷 retry 日志 + 节省 playwright 资源
    """
    # Phase 2D-2: 总开关（试用阶段可关）
    if not settings.crawl_enabled:
        logger.debug(f"采集已禁用 (AIMC_CRAWL_ENABLED=false), skip: {source.provider}")
        return

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


async def _check_key_rotation_task():
    """API-Sec-3: 每天扫一次 key 轮换 (30 天阈值), 飞书告警超期 key"""
    try:
        from scripts.check_key_rotation import check_key_rotation
        count = await check_key_rotation()
        logger.info(f"key 轮换检查完成: {count} 个告警")
    except Exception as e:
        logger.error(f"key 轮换检查失败: {e}", exc_info=True)
