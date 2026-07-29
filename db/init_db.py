"""数据库初始化模块

负责建表和初始数据导入。

改进历史：
- 2026-07-22: 加 ensure_migrations() 幂等迁移 (Phase 2A-2)
  - 老 DB 表已存在时，给 intel_sources 加 last_content 列
"""

import asyncio
import logging

from sqlalchemy import text

from db.engine import engine
from db.models import Base

logger = logging.getLogger(__name__)


async def init_db():
    """创建所有表（如果不存在）+ 跑幂等迁移"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_migrations()
    logger.info("数据库表初始化完成")


async def ensure_migrations():
    """增量 schema 迁移（幂等）

    用途：老 DB 已经 init 过、但 schema 演进时（加新列），不会重新建表。
    SQLAlchemy `create_all` 只建不存在的表，不会改已有表。
    所以新增列必须用 ALTER TABLE，且要手动检查列存在性。

    当前迁移项：
    - intel_sources.last_content (Phase 2A-2, 2026-07-22)
    - promotions.flagged / flagged_reason (Phase 2D-1, 2026-07-24)
    - accounts.api_key_ciphertext (API-Sec-1, 2026-07-29)
    - accounts.api_key → nullable=True (API-Sec-1 兼容老明文)
    - channels.provider_type / billing_mode (Phase 2.3, 2026-07-29)
    """
    async with engine.begin() as conn:
        # intel_sources.last_content
        result = await conn.execute(text("PRAGMA table_info('intel_sources')"))
        columns = {row[1] for row in result.fetchall()}
        if "last_content" not in columns:
            await conn.execute(text("ALTER TABLE intel_sources ADD COLUMN last_content TEXT"))
            logger.info("迁移: intel_sources 添加 last_content 列")
        else:
            logger.debug("迁移: intel_sources.last_content 已存在，跳过")

        # promotions.flagged / flagged_reason (Phase 2D-1)
        result = await conn.execute(text("PRAGMA table_info('promotions')"))
        columns = {row[1] for row in result.fetchall()}
        if "flagged" not in columns:
            await conn.execute(text("ALTER TABLE promotions ADD COLUMN flagged INTEGER DEFAULT 0"))
            logger.info("迁移: promotions 添加 flagged 列")
        else:
            logger.debug("迁移: promotions.flagged 已存在，跳过")
        if "flagged_reason" not in columns:
            await conn.execute(text("ALTER TABLE promotions ADD COLUMN flagged_reason TEXT"))
            logger.info("迁移: promotions 添加 flagged_reason 列")
        else:
            logger.debug("迁移: promotions.flagged_reason 已存在，跳过")

        # accounts.api_key_ciphertext (API-Sec-1, 2026-07-29)
        result = await conn.execute(text("PRAGMA table_info('accounts')"))
        columns = {row[1] for row in result.fetchall()}
        # 1) 放宽 api_key NOT NULL → 可空 (迁移期允许老明文存在)
        # SQLite 没法直接改 NOT NULL, 只能重建表. 但 alembic 没接, 暂时不动
        # 老 schema 是 NOT NULL, 我们的 model 改成 nullable=True 不影响已建表
        # 2) 加 ciphertext 列
        if "api_key_ciphertext" not in columns:
            await conn.execute(text("ALTER TABLE accounts ADD COLUMN api_key_ciphertext BLOB"))
            logger.info("迁移: accounts 添加 api_key_ciphertext 列 (API-Sec-1)")
        else:
            logger.debug("迁移: accounts.api_key_ciphertext 已存在，跳过")

        # channels.provider_type / billing_mode (Phase 2.3, 2026-07-29)
        # ALTER ADD COLUMN NOT NULL 必须带 DEFAULT (SQLite 限制)
        result = await conn.execute(text("PRAGMA table_info('channels')"))
        columns = {row[1] for row in result.fetchall()}
        if "provider_type" not in columns:
            await conn.execute(text(
                "ALTER TABLE channels ADD COLUMN provider_type TEXT NOT NULL DEFAULT 'official'"
            ))
            logger.info("迁移: channels 添加 provider_type 列 (Phase 2.3)")
        else:
            logger.debug("迁移: channels.provider_type 已存在，跳过")
        if "billing_mode" not in columns:
            await conn.execute(text(
                "ALTER TABLE channels ADD COLUMN billing_mode TEXT NOT NULL DEFAULT 'pay_per_token'"
            ))
            logger.info("迁移: channels 添加 billing_mode 列 (Phase 2.3)")
        else:
            logger.debug("迁移: channels.billing_mode 已存在，跳过")


async def drop_all():
    """删除所有表（仅开发/测试用）"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    logger.info("所有表已删除")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(init_db())
    print("数据库初始化成功！")
