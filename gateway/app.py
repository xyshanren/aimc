"""FastAPI 应用工厂

负责创建 app 实例、注册路由、挂载中间件和静态文件。
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

    # 启动情报采集调度器（Phase 3 启用）
    # from intelligence.scheduler import start_scheduler
    # await start_scheduler()

    logger.info(f"AIMC 已启动 -> http://{settings.host}:{settings.port}")
    yield

    # === 关闭阶段 ===
    logger.info("AIMC 关闭中...")
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
