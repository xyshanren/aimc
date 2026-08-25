"""FastAPI 应用工厂

负责创建 app 实例、注册路由、挂载中间件和静态文件。

改进历史：
- 2026-07-22: lifespan 启动情报层 scheduler (Phase 2A 收尾)
  - 启动：调 start_scheduler() 注册 6 个 source + 1 个 route cache refresh
  - 关闭：调 stop_scheduler() 优雅停机 + close_browser() 释放 Playwright
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from config import settings
from db.init_db import init_db

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # === 启动阶段 ===
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger.info("AIMC 启动中...")

    # 初始化数据库
    await init_db()

    # 加载路由缓存
    from gateway.router import route_engine
    await route_engine.refresh_cache()
    logger.info("路由缓存加载完成")

    # API-Sec-2: 加载告警 runtime settings cache
    from intelligence.alerter import refresh_settings_cache
    await refresh_settings_cache()

    # 启动情报采集调度器（Phase 2A 启用）
    # 注意：scheduler 启动是 fire-and-forget，第一次抓取会按 interval 触发
    # 如果未装 Playwright 浏览器二进制，第一次抓取会失败但不影响服务
    try:
        from intelligence.scheduler import start_scheduler
        await start_scheduler()
    except Exception as e:
        logger.error(f"启动情报层调度器失败: {e}", exc_info=True)
        # 不影响主服务继续跑（网关层独立可用）

    logger.info(f"AIMC 已启动 -> http://{settings.host}:{settings.port}")
    yield

    # === 关闭阶段 ===
    logger.info("AIMC 关闭中...")
    # 1. 停调度器 (不再派发新 task)
    try:
        from intelligence.scheduler import stop_scheduler, wait_inflight_fetches
        await stop_scheduler()
        # V4.8.4: 等 in-flight fetch 完成, 再关 browser (否则正在用的 context 会崩)
        await wait_inflight_fetches(timeout=30)
    except Exception as e:
        logger.warning(f"停情报层调度器失败: {e}")

    # 2. 关 Playwright 浏览器 (in-flight 已完成/取消, 安全关 browser + driver)
    try:
        from intelligence.fetcher import close_browser
        await close_browser()
    except Exception as e:
        logger.warning(f"关浏览器失败: {e}")

    # 2.5 V4.9.1: 关 probe client (释放 httpx 连接池, 防 FD 泄漏)
    try:
        from intelligence.channel_health import close_probe_client
        await close_probe_client()
    except Exception as e:
        logger.warning(f"关 probe client 失败: {e}")

    # 关 DB 引擎
    from db.engine import engine
    await engine.dispose()


def create_app() -> FastAPI:
    """创建 FastAPI 应用实例"""
    app = FastAPI(
        title="AIMC - AI Model Compass",
        description="轻量级 LLM 网关与优惠情报聚合工具",
        version="0.1.0",
        lifespan=lifespan,
    )

    # 注册网关路由
    from gateway.routes import router as gateway_router
    app.include_router(gateway_router)

    # 注册 Dashboard 路由
    from dashboard.routes import router as dashboard_router
    app.include_router(dashboard_router)

    # 挂载 Dashboard 静态文件
    static_dir = Path(__file__).parent.parent / "dashboard" / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # 注册中间件
    from gateway.middleware import RequestLoggingMiddleware
    app.add_middleware(RequestLoggingMiddleware)

    return app
