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
from datetime import datetime
from pathlib import Path
from typing import Optional

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from config import settings

# V6.5: 项目根目录 (原 _weekly_suggest_changes_task 硬编码 /home/lxc/aimc,
# 换部署路径即子进程起不来 → 周建议静默断更; 相对模块定位对任意部署路径成立)
_PROJECT_ROOT = str(Path(__file__).resolve().parents[1])

logger = logging.getLogger(__name__)

# V4.8.4: 追踪 run_source_now 派发的 in-flight task, 供 shutdown 优雅等待
_inflight_tasks: set[asyncio.Task] = set()


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

    scheduler = AsyncIOScheduler(jobstores={"default": jobstore})
    # 阶段2 (2026-08-14): scheduler 全局默认
    # - coalesce=True: 堆积任务合并到最新 1 次 (避免服务挂掉后补跑累积)
    # - max_instances=1: 同一 job 不并发 (避免同 provider 多 source 同时抓被限流)
    # - misfire_grace_time=300: 5 分钟内的延迟任务仍执行 (宽容)
    scheduler.job_defaults = {
        "coalesce": True,
        "max_instances": 1,
        "misfire_grace_time": 300,
    }
    return scheduler


# 模块级 scheduler 单例（持久化模式）
scheduler = _build_scheduler()


def _prune_stale_crawl_jobs(scheduler_obj, current_job_ids: set) -> int:
    """V6.5 S12 (production-review): 清理残留的 batch_crawl_* job.

    改 poll_interval 后旧 batch_crawl_{provider}_{old_interval} 永久留在
    SQLAlchemyJobStore 继续跑 → 同源重复采集/重复入库。只保留本轮注册的
    id 集合内的采集 job, 其余 batch_crawl_* 一律移除。

    Returns:
        移除数
    """
    removed = 0
    for job in list(scheduler_obj.get_jobs()):
        if job.id.startswith("batch_crawl_") and job.id not in current_job_ids:
            try:
                scheduler_obj.remove_job(job.id)
                removed += 1
                logger.info(f"清理残留采集 job: {job.id}")
            except Exception as e:
                logger.warning(f"清理残留采集 job 失败: {job.id} ({e})")
    return removed


async def start_scheduler():
    """启动情报采集调度器

    Phase 2C-2: jobs 持久化到 SQLite
    - 启动时自动 reload 上次未执行的 jobs
    - 重启服务后任务不丢（如果服务挂了，apscheduler_jobs 表里 next_run_time 仍存着）

    Phase 3.1: 调度策略优化
    - 按 source_type 推荐频率 (DEFAULT_POLL_INTERVALS) 作为 fallback
    - source.poll_interval 优先, 未填则按 type 取默认值
    - 不同 type 错开时间 (避免同时 fetch 撞限流)
    """
    from intelligence.sources import get_all_sources
    from intelligence.sources.base import DEFAULT_POLL_INTERVALS

    sources = get_all_sources()

    # 阶段2 (2026-08-14): 按 provider 合并成 batch job
    # - 每个厂商 1 个 batch job, 串联该 provider 所有 source_types
    # - 31 个 crawl_* 合并成 ~10 个 batch (deepseek/glm/qwen/siliconflow/...)
    # - 减少 apscheduler_jobs 表行数 + 减少错峰计算
    # - 保留每个 source 独立的 fire-and-forget 行为
    from collections import defaultdict
    # V4.8.3: 按 (provider, poll_interval) 分组调度, 替代旧的 min(poll_interval) 批量
    # 旧逻辑: 一个 provider 一个 batch job, 间隔=min(所有源 poll_interval)
    #   → 86400s 的 model_list 源被 900s 的 web_page 源拉低到每 15 分钟跑一次
    #   → model_list 源每天被采 96 次而非 1 次 → 无意义 401 告警刷屏
    # 新逻辑: 按 poll_interval 分组, 同间隔的源共享一个 job
    #   → 900s 源每 15 分钟跑, 86400s 源每天跑一次, 互不干扰
    by_provider_interval: dict[tuple[str, int], list] = defaultdict(list)
    for source in sources:
        interval = source.poll_interval or DEFAULT_POLL_INTERVALS.get(source.source_type, 900)
        by_provider_interval[(source.provider, interval)].append(source)

    # 删旧 per-provider batch job (一次性迁移: batch_crawl_{provider} → batch_crawl_{provider}_{interval})
    # 同时删更早的 per-source job
    for source in sources:
        for old_job_id in (
            f"crawl_{source.provider}_{source.source_type}",
            f"batch_crawl_{source.provider}",
        ):
            try:
                scheduler.remove_job(old_job_id)
            except Exception:
                pass  # job 不存在 OK

    for (provider, interval), group_sources in by_provider_interval.items():
        scheduler.add_job(
            _run_provider_batch,
            trigger=IntervalTrigger(
                seconds=interval,
                jitter=min(300, max(1, interval // 20)),
            ),
            args=[provider, group_sources],
            id=f"batch_crawl_{provider}_{interval}",
            name=f"批量采集: {provider} ({interval}s, {len(group_sources)} sources)",
            replace_existing=True,
        )
        logger.info(
            f"注册批量采集: {provider} ({len(group_sources)} sources, "
            f"间隔 {interval}s)"
        )

    # V6.5 S12 (production-review): 清理残留采集 job — 上面只删两代更旧的 id,
    # 改 poll_interval 后旧 batch_crawl_{provider}_{old_interval} 会永久留在
    # SQLAlchemyJobStore 继续跑 → 同源重复采集/重复入库。按本轮新 id 集合
    # 兜底清理一切不匹配的 batch_crawl_*。
    _prune_stale_crawl_jobs(
        scheduler,
        {f"batch_crawl_{p}_{i}" for (p, i) in by_provider_interval},
    )

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

    # Phase 4 E3 (2026-07-29): 每天跑 1 次跨源交叉验证
    scheduler.add_job(
        _cross_source_detection_task,
        trigger=IntervalTrigger(hours=24),
        id="cross_source_detection",
        name="跨源交叉验证",
        replace_existing=True,
    )

    # Phase 4 v4.3.1 (2026-08-13): 月度跑 1 次官方源审查 (防止 commit 后漂移未察觉)
    from apscheduler.triggers.cron import CronTrigger
    scheduler.add_job(
        _monthly_official_audit_task,
        trigger=CronTrigger.from_crontab("0 6 1 * *"),  # 每月 1 日 6:00 AM
        id="monthly_official_audit",
        name="月度官方源审查",
        replace_existing=True,
    )

    # V4.4 告警检查 (2026-08-21): 每 6 小时跑 1 次配额/账号/成本告警
    # 之前 check_all_alerts 从未被注册 (死代码), 现补上调度
    from intelligence.v44_alerts import check_all_alerts
    scheduler.add_job(
        check_all_alerts,
        trigger=IntervalTrigger(hours=6),
        id="v44_alerts_check",
        name="V4.4 告警检查 (配额/账号/成本)",
        replace_existing=True,
    )

    # 方案A (2026-08-14): 每周一 09:00 跑 suggest_changes
    # 跟周报生成对齐: 周一 09:00 跑完 suggest_changes, 周报 09:00 拉取 pending 建议
    scheduler.add_job(
        _weekly_suggest_changes_task,
        trigger=CronTrigger.from_crontab("0 9 * * 1"),  # 每周一 09:00
        id="weekly_suggest_changes",
        name="周建议生成 (方案A)",
        replace_existing=True,
    )

    # V4.9 (2026-08-25): Channel 智能探活 (基于 cooldown_manager.accounts_due_for_probe)
    # 替代 V4.8.7 P7 的「失败 → 60s 后路由重试」机制
    # 间隔: AIMC_HEALTH_PROBE_INTERVAL (默认 60s), 实际只探 needs_human=False 且到期的 account
    from intelligence.channel_health import channel_health_probe, cooldown_cleanup_task
    scheduler.add_job(
        channel_health_probe,
        trigger=IntervalTrigger(seconds=settings.health_probe_interval),
        id="channel_health_probe",
        name="Channel 智能探活 (基于状态机)",
        replace_existing=True,
    )

    # V4.9: cooldown_manager.cleanup() 周期化 (调研发现从未被自动调过)
    scheduler.add_job(
        cooldown_cleanup_task,
        trigger=IntervalTrigger(minutes=5),
        id="cooldown_cleanup",
        name="Cooldown 过期清理",
        replace_existing=True,
    )

    # V4.9.1 防护 #3: account_health_log 表 retention (每天凌晨 3 点删 7 天前)
    # 防止探活日志表无限增长 (1 channel 每天 1440 条, 不清理 1 年 52 万条)
    # 注意: APScheduler 用 SQLAlchemy 持久化, 回调不能是 lambda (无法 pickle)
    # 必须用模块级函数
    from intelligence.channel_health import account_health_log_cleanup_task
    scheduler.add_job(
        account_health_log_cleanup_task,
        trigger=CronTrigger.from_crontab("0 3 * * *"),
        id="account_health_log_cleanup",
        name="探活日志清理 (保留 N 天)",
        replace_existing=True,
    )

    # Phase 3 体验 (2026-09-05): 优惠情报过期清理 (用户确认保留期 30 天)
    # 1) end_date 已过的 active/upcoming 刷成 expired; 2) expired 超 30 天物理删除
    from intelligence.promotion_cleanup import cleanup_expired_promotions_task
    scheduler.add_job(
        cleanup_expired_promotions_task,
        trigger=CronTrigger.from_crontab("20 4 * * *"),
        id="promotion_cleanup",
        name="优惠情报过期清理 (保留 30 天)",
        replace_existing=True,
    )

    # V4.9.3: request_transcripts 表 retention (每 6 小时清理超期转录)
    # 转录日志默认关闭, 只有诊断时才开启; 开启后按 retention_hours 自动清理防膨胀
    from intelligence.channel_health import transcript_log_cleanup_task
    scheduler.add_job(
        transcript_log_cleanup_task,
        trigger=IntervalTrigger(hours=6),
        id="transcript_log_cleanup",
        name="转录日志清理 (保留 N 小时)",
        replace_existing=True,
    )

    # A-2 (2026-09-08): 系统级盲区审计 — 全部在采源 7 天零变化检测 → 告警
    # 9/8 实证: 错误页被当内容 → 14 天零变化检测无任何告警; 本任务是最后防线
    scheduler.add_job(
        intel_blindspot_audit_task,
        trigger=IntervalTrigger(hours=6),
        id="intel_blindspot_audit",
        name="情报系统盲区审计 (A-2)",
        replace_existing=True,
    )

    # 阶段2 (2026-08-14): 注册 apscheduler event listener
    # 同步写 job_health 表 (last_run_at / last_success_at / last_error_at / last_duration_ms)
    # 不阻塞 scheduler, 失败仅 log
    def _record_job_event(event):
        """apscheduler event listener (sync): 写 job_health

        注意: 这是 sync 函数, 用 sync db session 写 (避免 asyncio 嵌套)
        """
        from datetime import datetime as _dt
        import sys as _sys
        print(f"[job_health LISTENER] FIRED for {event.job_id}, success={event.exception is None}", file=_sys.stderr, flush=True)
        try:
            from db.engine import sync_engine as _sync_engine
            from sqlalchemy.orm import Session as _SyncSession
            from db.models import JobHealth as _JobHealth
            job_id = event.job_id
            now = _dt.now()
            success = event.exception is None
            # 实际事件字段是 scheduled_run_time (不是 scheduled_call_time)
            duration_ms = None
            if hasattr(event, "scheduled_run_time") and event.scheduled_run_time:
                duration_ms = int(event.scheduled_run_time.timestamp() * 1000)

            with _SyncSession(_sync_engine) as s:
                jh = s.get(_JobHealth, job_id)
                if jh is None:
                    jh = _JobHealth(job_id=job_id, total_runs=0, total_successes=0, total_failures=0)
                    s.add(jh)
                jh.last_run_at = now
                jh.total_runs = (jh.total_runs or 0) + 1
                if success:
                    jh.last_success_at = now
                    jh.last_error_at = None
                    jh.last_error_msg = None
                    jh.last_duration_ms = duration_ms
                    jh.total_successes = (jh.total_successes or 0) + 1
                else:
                    jh.last_error_at = now
                    jh.last_error_msg = str(event.exception)[:500]
                    jh.total_failures = (jh.total_failures or 0) + 1
                s.commit()
            print(f"[job_health] wrote for {job_id}", file=_sys.stderr, flush=True)
        except Exception as e:
            print(f"[job_health] 写失败 (非致命): {e}", file=_sys.stderr, flush=True)
            logger.debug(f"[job_health] 写失败 (非致命): {e}")

    scheduler.add_listener(_record_job_event, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)
    logger.info("[阶段2] 注册 apscheduler event listener -> 写 job_health")

    scheduler.start()
    logger.info(
        f"调度器已启动 (持久化模式), 共 {len(sources) + 4} 个定时任务, "
        f"jobstore={settings.db_path}"
    )


async def stop_scheduler():
    """停止调度器"""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("调度器已停止")


async def wait_inflight_fetches(timeout: float = 30):
    """V4.8.4: 优雅等待 in-flight fetch task 完成 (shutdown 时调用)

    stop_scheduler 只停调度器 (不再派发新 task), 但已通过 run_source_now 或
    _run_provider_batch 派发的 task 仍在运行. 若此时直接 close_browser 会
    导致正在使用的 context/page 崩溃. 本函数给它们一个优雅退出的窗口.
    """
    if not _inflight_tasks:
        return
    logger.info(f"等待 {len(_inflight_tasks)} 个 in-flight fetch task (timeout={timeout}s)")
    try:
        await asyncio.wait_for(
            asyncio.gather(*_inflight_tasks, return_exceptions=True),
            timeout=timeout,
        )
        logger.info("所有 in-flight fetch task 已完成")
    except asyncio.TimeoutError:
        logger.warning(f"等待 in-flight task 超时 ({timeout}s), 强制取消 {len(_inflight_tasks)} 个")
        for t in list(_inflight_tasks):
            t.cancel()


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
        # V4.8.4: 追踪 in-flight task (旧: 纯 fire-and-forget, shutdown 无法等待)
        task = asyncio.create_task(_run_source_task(source))
        _inflight_tasks.add(task)
        task.add_done_callback(_inflight_tasks.discard)
        count += 1

    logger.info(f"手动触发 {provider}: 启动 {count} 个 task")
    return count


async def _run_provider_batch(provider: str, sources: list):
    """阶段2 (2026-08-14): 批量采集 1 个 provider 的所有 sources

    跟 _run_source_task 一样的 fire-and-forget 行为, 但:
    - 同一个 provider 的所有 source 一起触发 (announcement + model_list + price_aggregator)
    - 顺序触发 (避免同 provider 多 source 同时 fetch 撞限流)
    - 单个 source 失败不影响其他

    V6.5 S7 (production-review): 自身注册进 _inflight_tasks — 旧实现只有
    run_source_now 手动任务被登记, 定时 batch job 关停时不被 wait_inflight_fetches
    等待 → 正在抓取/落库的采集被 engine.dispose() 打断 (hash 已更新内容未入库)。

    Args:
        provider: 厂商标识 (e.g. "deepseek")
        sources: 该 provider 的 Source 列表
    """
    task = asyncio.current_task()
    if task is not None:
        _inflight_tasks.add(task)
        task.add_done_callback(_inflight_tasks.discard)
    logger.info(f"[batch_crawl_{provider}] 启动 {len(sources)} 个 source")
    for source in sources:
        try:
            await _run_source_task(source)
        except Exception as e:
            logger.warning(
                f"[batch_crawl_{provider}] source {source.provider}/{source.source_type} "
                f"失败: {e}"
            )
            # 继续下一个 source (不阻断)
            continue


async def _run_source_task(source):
    """执行单个采集源的任务

    Phase 2C-1: 接入 handle_fetch_error 告警
    - fetch 失败时调 handle_fetch_error（连续 3 次告警 + disable）
    - 外层 Exception 仍然只 log（开发者 bug 不应刷 Telegram）

    Phase 2D-2: 加 crawl_enabled 总开关
    - 试用阶段关掉 → _run_source_task 第一行 short-circuit
    - 避免刷 retry 日志 + 节省 playwright 资源

    Phase 3.6 (2026-08-06): API 结构化抽取
    - 如 source.api_endpoint 设置: 优先用 fetch_models_via_api
    - 4xx (401/403/404) → 视为不可重试, 降级到 HTML 路径 (保留 fallback)
    - 5xx/timeout → 重试 3 次后降级 HTML
    - API 成功 → 写入 promotions.raw_data (kind="model_metadata", models=[{model_id, ...}])
    """
    # Phase 2D-2: 总开关（试用阶段可关）
    if not settings.crawl_enabled:
        logger.debug(f"采集已禁用 (AIMC_CRAWL_ENABLED=false), skip: {source.provider}")
        return

    # V4.8.3: per-source 禁用检查 (dashboard toggle 写 DB status=disabled)
    try:
        from db.engine import async_session_factory
        from db import crud
        async with async_session_factory() as session:
            db_src = await crud.get_intel_source_by_provider_url(
                session, source.provider, source.url
            )
            if db_src and db_src.status == "disabled":
                logger.debug(
                    f"源已禁用 (dashboard toggle), skip: "
                    f"{source.provider}/{source.source_type}"
                )
                return
    except Exception as e:
        logger.debug(f"per-source 禁用检查失败 (非阻断): {e}")

    # V4.8.3: 自适应降频 — 连续无变化的源降低采集频率
    # consecutive_no_change >= 4 → 4x 间隔 (15min 源 → 1h)
    # consecutive_no_change >= 10 → 8x 间隔 (15min 源 → 2h)
    # 有变化时 differ._update_source_hash 会归零 consecutive_no_change
    try:
        from intelligence.differ import load_no_change_count
        no_change = await load_no_change_count(source)
        if no_change >= 4:
            interval = source.poll_interval or 900
            multiplier = 8 if no_change >= 10 else 4
            effective_interval = interval * multiplier
            from datetime import datetime as _dt
            from db.engine import async_session_factory
            from db import crud
            async with async_session_factory() as session:
                db_src = await crud.get_intel_source_by_provider_url(
                    session, source.provider, source.url
                )
                last_check = db_src.last_check_at if db_src else None
            if last_check and (_dt.now() - last_check).total_seconds() < effective_interval:
                logger.debug(
                    f"自适应降频: {source.provider} 连续 {no_change} 次无变化, "
                    f"距上次检查不足 {effective_interval}s, 跳过"
                )
                return
    except Exception as e:
        logger.debug(f"自适应降频检查失败 (非阻断): {e}")

    try:
        from intelligence.fetcher import fetch_page_with_error
        from intelligence.differ import has_changed
        from intelligence.healer import handle_parse_result, handle_fetch_error
        from intelligence.sources.api_models import fetch_models_via_api, FetchApiError

        content = None
        error = None

        # Phase 3.6: 优先 API 结构化抽取 (如配置了)
        if getattr(source, "api_endpoint", None):
            api_key = await _resolve_api_key(source)
            # V4.8.3: 无可用 API key + 无 HTML fallback → 静默跳过, 不告警
            # 这 7 个 model_list 源 (deepseek/glm/qwen/stepfun/sensenova/hunyuan/moonshot)
            # 配了 api_endpoint 但无对应 channel 的 API key → 每次 401 → 告警刷屏。
            # 配了 key 后自动恢复采集 (api_key 非 None 即走正常流程)。
            if not api_key and not source.url:
                logger.info(
                    f"{source.provider} model_list 源无可用 API key, 静默跳过 "
                    f"(配置 api_key_from='{source.api_key_from}' 对应 channel 后自动恢复)"
                )
                return
            try:
                model_ids = await fetch_models_via_api(
                    base_url=source.api_endpoint,
                    api_key=api_key,
                )
                # 把 model_ids 包装成 model_sync 期望的 raw_data JSON 格式
                # (Phase 3.3 已实现的 process_promotion 消费)
                import json as _json
                models_meta = [
                    {
                        "model_id": mid,
                        "family": source.provider,
                        "source": "api_models",
                        # V4.8.6: 采集来源渠道, 透传到 ModelCapabilitiesLog.provider
                        "source_provider": source.provider,
                        # confidence=1.0 因 API 抽取是 100% 确定性
                        "confidence": 1.0,
                        "confidence_level": "high",
                    }
                    for mid in model_ids
                ]
                content = _json.dumps({
                    "_intelligence_kind": "model_metadata",
                    "provider": source.provider,
                    "models": models_meta,
                }, ensure_ascii=False)
                logger.info(
                    f"API 抽取成功: {source.provider} → {len(model_ids)} 个 model"
                )
                # Phase 2a+ fix (2026-08-13): 同时把 model_ids 同步到硅基流动 channel
                # price_snapshots. 让 /v1/models 在重启后立刻可见, 不需要等 router cache
                # 仅硅基流动启用, 其他厂商暂不动 (避免污染)
                if source.provider == "siliconflow":
                    try:
                        from db.engine import async_session_factory
                        from scripts._siliconflow_sync import sync_siliconflow_models_to_price_snapshots
                        async with async_session_factory() as session:
                            added = await sync_siliconflow_models_to_price_snapshots(session, model_ids)
                        if added:
                            logger.info(
                                f"硅基流动 model 列表同步: 新增 {added} 条 price_snapshots"
                            )
                    except Exception as e:
                        logger.warning(f"硅基流动 price_snapshots 同步失败 (非阻断): {e}")
            except FetchApiError as e:
                # 401/403/404 → 不可重试, 降级 HTML
                # 5xx/timeout → 已重试完仍失败, 降级 HTML
                logger.warning(
                    f"API 抽取失败 ({source.provider}, status={e.status_code}): "
                    f"{e} → 降级 HTML 抓取"
                )
                # 让 content/error 走下面 HTML 分支
                error = f"API 失败, 降级 HTML: {e}"

        # HTML 路径 (fallback 或 api_endpoint 未设置)
        # Phase 3.6 增强: 跳过空 url (新厂商 source 只配 api_endpoint 时)
        if content is None and source.url:
            # V6.14 (2026-09-27): 接线 Phase 2B-3 等待策略 — 旧调用只传 url,
            # source.wait_until / wait_for 全项目零消费者 (死配置), SPA 站只能拿到
            # 未水合 SSR 壳 (sensenova /docs 实证: 15.6KB 壳 clean 后 "$/$")
            content, error = await fetch_page_with_error(
                source.url,
                wait_until=getattr(source, "wait_until", None) or "domcontentloaded",
                wait_for=getattr(source, "wait_for", None),
                wait_for_timeout=getattr(source, "wait_for_timeout", None) or 10000,
            )
            if not content:
                logger.warning(f"采集失败: {source.provider} - {error}")
                # 调 handle_fetch_error：累加 error_count + 按规则告警
                await handle_fetch_error(source, error or "未知错误")
                return

            # A-2 (2026-09-08): 内容健康检查 — 错误页不进 differ
            # 错误页字节级稳定 → 入库 last_content 后会永久"无变化"
            # (9/8 实证: 多源错误页导致全系统 14 天零变化检测且无告警)。
            # 走 handle_fetch_error 复用既有 error_count/禁用/恢复机制;
            # API 结构化内容 (JSON) 不检查, 避免误伤小模型列表
            from intelligence.content_guard import classify_content
            healthy, guard_reason = classify_content(content)
            if not healthy:
                logger.warning(
                    f"内容健康检查不通过: {source.provider} - {guard_reason}"
                )
                await handle_fetch_error(
                    source, f"{guard_reason} (head={content[:150]!r})"
                )
                return
        elif content is None:
            # api_endpoint 失败且没配 HTML url (新厂商 source 常见)
            logger.warning(
                f"采集失败: {source.provider} - API 失败且无 HTML url 配置, "
                f"已跳过本次采集"
            )
            await handle_fetch_error(source, "API 失败且无 HTML url 配置")
            return

        # 2. Diff 检测
        # V6.5 S4 (production-review): peek/commit 两段式 — 旧实现先落 hash
        # 再解析, 解析失败(LLM 超时/异常/无规则)即把该次变化永久吞掉(页面
        # 字节不再变则永不重试)。peek 判定, 解析成功才提交 hash。
        changed = await has_changed(source, content, commit=False)
        if not changed:
            logger.debug(f"无变化，跳过: {source.provider}")
            return

        logger.info(f"检测到变化: {source.provider} -> 触发自愈流程")

        # 3. 自愈流程（AI 解析 → CSS 降级 → 告警）
        # Phase 2B-2: handle_parse_result 接收原始 content，自己跑 AI
        # V6.5 S4: 返回 True(AI/CSS 任一入库成功)才提交新 hash; False 保留
        # 旧 hash, 下轮同内容重新判定"有变化"自动重试
        from intelligence.differ import commit_source_hash
        parsed_ok = await handle_parse_result(content, source)
        if parsed_ok:
            await commit_source_hash(source, content)
        else:
            logger.warning(
                f"解析失败, 保留旧 hash 下轮重试: {source.provider}"
            )

    except Exception as e:
        # 外层 Exception 是代码 bug，开发者排查用
        # 不发 Telegram 避免刷屏（设计选择）
        logger.error(f"采集任务异常: {source.provider} - {e}", exc_info=True)


async def _resolve_api_key(source) -> Optional[str]:
    """从渠道/账号读 api_key (解密), 给 api_endpoint 鉴权用

    Phase 3.6: 优先用 source.api_key_from 对应 provider 的 active channel 的 api_key
    - 如果 provider 没 channel → 返回 None (按公开 endpoint 走)
    - API-Sec-1: api_key 在 DB 加密存储, 通过 try_decrypt 拿明文

    2026-09-09: V4.8 多账号迁移后 key 实际存 accounts 表 (生产 channels 表无
    key 列, 渠道级读取恒空 → 7 个 API 型 model_list 源恒静默跳过, 24h ~342 次)。
    渠道无 key 时回退该渠道第一个 active 账号, 解密路径与
    gateway.router._resolve_account_key 一致。
    """
    if not getattr(source, "api_key_from", None):
        return None

    try:
        from db.engine import async_session_factory
        from db.crud import list_active_channels_by_provider, get_accounts_by_channel
        from security.crypto import try_decrypt

        provider = source.api_key_from
        async with async_session_factory() as session:
            channels = await list_active_channels_by_provider(session, provider)

        if not channels:
            logger.debug(f"{provider} 无 active channel, 走公开 endpoint")
            return None

        # 取第一个 active channel (通常个人用只有 1 个)
        ch = channels[0]
        ciphertext = getattr(ch, "api_key_ciphertext", None)
        if ciphertext:
            plaintext = try_decrypt(ciphertext, settings.aimc_master_key)
            if plaintext:
                return plaintext

        # 渠道级无 key → 回退该渠道第一个 active 账号 (key 实际存放处)
        async with async_session_factory() as session:
            accounts = await get_accounts_by_channel(session, ch.id)
        for acc in accounts:
            if getattr(acc, "status", None) != "active":
                continue
            if acc.api_key_ciphertext is not None:
                plaintext = try_decrypt(acc.api_key_ciphertext, settings.aimc_master_key)
                if plaintext:
                    return plaintext
            if acc.api_key:
                # 明文 (deprecated, 仅迁移期老库)
                return acc.api_key

        logger.debug(f"{provider} 渠道与账号均无可用 key, 走公开 endpoint")
        return None
    except Exception as e:
        logger.warning(f"读取 {source.api_key_from} api_key 失败: {e}, 走公开 endpoint")
        return None


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


async def _cross_source_detection_task():
    """Phase 4 E3: 每天 1 次跨源交叉验证，检测后入库到 cross_source_discrepancies"""
    try:
        from intelligence.cross_source_validator import detect_and_record
        summary = await detect_and_record()
        logger.info(
            f"跨源验证完成: models={summary['total_models']} "
            f"分歧={summary['with_discrepancy']} 入库={summary['recorded']}"
        )
    except Exception as e:
        logger.error(f"跨源验证失败: {e}", exc_info=True)


async def _monthly_official_audit_task():
    """Phase 4 v4.3.1 (2026-08-13): 月度官方源审查

    爬 5 家厂商官方 API 文档 (DeepSeek/Kimi/MiniMax/Qwen/GLM),
    跟 model_registry model_id 集合 diff,
    发现漂移 → Bark/Server酱/Feishu 告警.

    复用 scripts/check_model_consistency.check_official_consistency
    """
    try:
        from db.engine import async_session_factory
        from scripts.check_model_consistency import check_official_consistency

        async with async_session_factory() as session:
            diffs = await check_official_consistency(session)

        # 统计漂移
        official_only_total = sum(len(d.official_only) for d in diffs)
        registry_only_total = sum(len(d.registry_only) for d in diffs)
        fetch_errors = sum(1 for d in diffs if d.fetch_error)

        logger.info(
            f"月度官方源审查完成: 官方漂移 {official_only_total} 个, "
            f"registry 漂移 {registry_only_total} 个, 爬取失败 {fetch_errors} 个"
        )

        # 有漂移或爬取失败 → 告警
        if official_only_total > 0 or registry_only_total > 0 or fetch_errors > 0:
            try:
                from intelligence.alerter import send_alert
                msg = (
                    f"月度官方源审查发现漂移:\n"
                    f"- 官方有 / registry 无: {official_only_total} 个 model\n"
                    f"- registry 有 / 官方无: {registry_only_total} 个 model\n"
                    f"- 爬取失败: {fetch_errors} 个厂商\n"
                    f"建议: 跑 python scripts/check_model_consistency.py --check official 查看详情"
                )
                await send_alert(
                    msg,
                    key="monthly_audit:official_source_drift",
                    silence_seconds=86400,
                )
            except Exception as e:
                logger.warning(f"月度审查告警发送失败 (非阻断): {e}")
    except Exception as e:
        logger.error(f"月度官方源审查失败: {e}", exc_info=True)


async def _weekly_suggest_changes_task():
    """方案A (2026-08-14): 每周一 09:00 跑 suggest_changes

    流程:
    1. 跑 intelligence/analytics/suggest_changes.py (默认 shadow mode 写 log)
    2. 写入 db (status=pending) 等 dashboard review
    3. 周报 09:00 拉取 pending/approved 建议, 推到 Bark/Feishu

    设计:
    - 跟周报生成同时间 (周一 09:00) 保证建议能在周报里看到
    - shadow mode 默认开: 不写 db, 只写 auto_apply_shadow.log (为远期自动应用积累数据)
    - 半年后看 shadow log 跟人工批准的一致率, 决定是否开真自动应用

    失败模式:
    - 最小数据门槛不满足 (usage_logs < 30) → 跳过, 不报错
    - 数据源 query 失败 → log error, 下周重试
    """
    try:
        # 用 subprocess 跑 (跟 scripts/ 风格一致, 不在 scheduler 进程内)
        import subprocess
        import sys as _sys
        result = subprocess.run(
            [_sys.executable, "-m", "intelligence.analytics.suggest_changes",
             "--apply"],  # 生产模式: 写 db (周报需要读)
            cwd=_PROJECT_ROOT,  # V6.5: 相对模块定位 (原硬编码 /home/lxc/aimc, 换部署路径即静默断)
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            logger.info("方案A: 周建议生成完成 (--apply)")
            # 提取建议数 (从 stdout 找 "总计: X model + Y group")
            for line in result.stdout.splitlines():
                if "总计:" in line:
                    logger.info(f"  {line.strip()}")
                    break
        else:
            logger.error(f"方案A: 周建议生成失败 (rc={result.returncode})")
            logger.error(f"  stdout: {result.stdout[-500:]}")
            logger.error(f"  stderr: {result.stderr[-500:]}")
    except subprocess.TimeoutExpired:
        logger.error("方案A: 周建议生成超时 (>120s)")
    except Exception as e:
        logger.error(f"方案A: 周建议生成异常: {e}", exc_info=True)


# A-2 (2026-09-08): 盲区判定阈值 — 全部在采HTML源距最后变化检测超过此天数 → 失明
# 依据: 官网优惠月级变化是常态, 单源 7 天无变化不告警 (进周报);
#       但全部源 7 天零变化 = 系统级失明 (9/8 实证: 14 天零变化无人知晓)
BLINDSPOT_DAYS = 7
# 参与盲区判定的最小在采源数 (避免样本过小时误报)
BLINDSPOT_MIN_SOURCES = 3
# "在采" 判定: 距上次检查在此窗口内 (说明爬虫还活着, 而不是停摆)
BLINDSPOT_ACTIVE_WINDOW_HOURS = 48


async def intel_blindspot_audit_task():
    """A-2 (2026-09-08): 系统级盲区审计 — 每 6h 检查"情报系统是否失明"

    故障模式: 每个源局部都"正常" (采集成功 + 无变化), 但全局已失明 —
    9/8 实证: 错误页字节级稳定 → differ 判无变化 → 14 天全系统零变化检测,
    全程无告警。本任务是最后一道防线: 站在全局视角看"系统多久没产出过变化了"。

    判据 (三者同时满足才告警, throttle 3 天):
    1. 参与 HTML 采集的 active 源 (web_page/announcement/price_aggregator) 数量
       >= BLINDSPOT_MIN_SOURCES — 排除 model_list (缺 key 空转) / benchmark (周级) /
       从未检查过的源
    2. 全部参与源 last_check_at 距今 < 48h — 爬虫还在正常跑
    3. 全部参与源 last_changed_at 距今 > BLINDSPOT_DAYS 天 — 但很久没见到任何变化
    """
    try:
        from datetime import timedelta

        from sqlalchemy import select

        from db.engine import async_session_factory
        from db.models import IntelSource

        now = datetime.now()
        participating_types = ("web_page", "announcement", "price_aggregator")
        async with async_session_factory() as session:
            rows = (
                await session.execute(
                    select(IntelSource).where(
                        IntelSource.status == "active",
                        IntelSource.source_type.in_(participating_types),
                    )
                )
            ).scalars().all()

        running = []
        for r in rows:
            if r.last_check_at is None:
                continue  # 从未检查过 (新源/空转源), 不参与判定
            age_hours = (now - r.last_check_at).total_seconds() / 3600
            if age_hours > BLINDSPOT_ACTIVE_WINDOW_HOURS:
                continue  # 长期未检查 (爬虫没跑它), 不参与判定
            running.append(r)

        if len(running) < BLINDSPOT_MIN_SOURCES:
            logger.debug(
                f"盲区审计: 在采源不足 {BLINDSPOT_MIN_SOURCES} 个"
                f" (实际 {len(running)}), 跳过判定"
            )
            return

        changed_since = now - timedelta(days=BLINDSPOT_DAYS)
        stale = [r for r in running if not r.last_changed_at or r.last_changed_at < changed_since]
        if len(stale) < len(running):
            logger.debug(
                f"盲区审计: {len(running)} 个在采源中 {len(running) - len(stale)} 个"
                f"近 {BLINDSPOT_DAYS} 天有变化, 系统健康"
            )
            return

        lines = [f"⚠️ 情报系统疑似失明: {len(running)} 个在采源已 {BLINDSPOT_DAYS} 天零变化检测"]
        for r in running:
            days_stale = (
                (now - r.last_changed_at).days if r.last_changed_at else "从未"
            )
            lines.append(
                f"- {r.provider}/{r.source_type}: 连续无变化 {r.consecutive_no_change} 次, "
                f"最后变化 {days_stale} 天前, url={r.url}"
            )
        lines.append("建议: 检查采集源是否抓到错误页 (content_guard 告警) / 页面改版 / 反爬")
        from intelligence.alerter import send_alert
        await send_alert(
            "\n".join(lines),
            key="blindspot_audit:intel_blind",
            silence_seconds=3 * 86400,
        )
        logger.warning(lines[0])
    except Exception as e:
        logger.error(f"盲区审计失败: {e}", exc_info=True)
