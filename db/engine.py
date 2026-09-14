"""SQLAlchemy 异步引擎初始化

使用 aiosqlite 驱动，WAL 模式提升并发读性能。

P0-8 (2026-08-14): 加独立 sync_engine (阶段 2 apscheduler event listener 需要)
- engine.sync_engine 跟 async engine 共享 greenlet 上下文, 跑 sync Session 会报
  "greenlet_spawn has not been called"
- sync_engine 独立, 跟 async engine 完全解耦, 可在 async 上下文里安全用 sync Session
- 主要用途: apscheduler event listener 写 job_health (P0-8 修复)
"""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy import create_engine, event, text

from config import settings

# 异步引擎
engine = create_async_engine(
    settings.db_url,
    echo=False,
    future=True,
)

# P0-8: 独立 sync engine (给 apscheduler event listener / 一次性脚本用)
# 跟 engine.sync_engine (从 async 引擎桥接) 不同 — 真正的独立 Engine,
# 不会触发 "greenlet_spawn has not been called" 错误
sync_engine = create_engine(
    settings.db_url.replace("sqlite+aiosqlite", "sqlite"),
    echo=False,
    future=True,
    connect_args={"check_same_thread": False},  # apscheduler 多线程安全
)

# 会话工厂
async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


@event.listens_for(sync_engine, "connect")
def _set_sqlite_pragma_sync(dbapi_conn, connection_record):
    """P0-8: sync engine PRAGMA (跟 async 一致).

    V6.5 S11 (production-review): 原来只设 WAL — job_health 经 sync_engine 写
    (scheduler event listener), 与 async 侧并发写时缺 busy_timeout 易
    database is locked, 且异常被 listener 吞掉 → job_health 数据静默缺失。
    (foreign_keys 不在此处开: 与 async 侧保持差异最小化, 避免 listener 存量
    写入行为变化; async 侧已开不受影响)
    """
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


@event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragma(dbapi_conn, connection_record):
    """SQLite 连接初始化时设置 PRAGMA"""
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


async def get_session() -> AsyncSession:
    """FastAPI 依赖注入：获取数据库会话"""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
