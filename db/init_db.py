"""数据库初始化模块

负责建表和初始数据导入。
"""

import asyncio
import logging

from db.engine import engine
from db.models import Base

logger = logging.getLogger(__name__)


async def init_db():
    """创建所有表（如果不存在）"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("数据库表初始化完成")


async def drop_all():
    """删除所有表（仅开发/测试用）"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    logger.info("所有表已删除")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(init_db())
    print("数据库初始化成功！")
