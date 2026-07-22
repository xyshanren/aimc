"""SQLAlchemy 异步引擎初始化

使用 aiosqlite 驱动，WAL 模式提升并发读性能。
"""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy import event, text

from config import settings

# 创建异步引擎
engine = create_async_engine(
    settings.db_url,
    echo=False,
    future=True,
)

# 会话工厂
async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


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
