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
    - accounts.last_rotated_at / expires_at (API-Sec-3, 2026-07-29)
    - channels.provider_type / billing_mode (Phase 2.3, 2026-07-29)
    - settings_runtime 表 (API-Sec-2, 2026-07-29) — 新建表, create_all 自动
    - usage_logs.group_id (Phase 3.A, 2026-07-29) — Dashboard 命中率聚合
    - model_group_members.is_active / removed_at / removed_reason / removed_by (P1-2, 2026-08-06) — 软删除
    - model_feedback_summary (V4.4.A) — 模型周反馈汇总表
        - accounts.notes (V4.5 fix, 2026-08-08) — add_account.py --notes 缺列补 migration
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

        # accounts.last_rotated_at / expires_at (API-Sec-3, 2026-07-29)
        # 用同一份 columns (刚 query 过)
        if "last_rotated_at" not in columns:
            await conn.execute(text("ALTER TABLE accounts ADD COLUMN last_rotated_at DATETIME"))
            logger.info("迁移: accounts 添加 last_rotated_at 列 (API-Sec-3)")
        else:
            logger.debug("迁移: accounts.last_rotated_at 已存在，跳过")
        if "expires_at" not in columns:
            await conn.execute(text("ALTER TABLE accounts ADD COLUMN expires_at DATETIME"))
            logger.info("迁移: accounts 添加 expires_at 列 (API-Sec-3)")
        else:
            logger.debug("迁移: accounts.expires_at 已存在，跳过")

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

        # usage_logs.group_id (Phase 3.A, 2026-07-29)
        # 用于 Dashboard 命中率聚合查询, 老数据全部 NULL (精确模型路由)
        result = await conn.execute(text("PRAGMA table_info('usage_logs')"))
        columns = {row[1] for row in result.fetchall()}
        if "group_id" not in columns:
            await conn.execute(text(
                "ALTER TABLE usage_logs ADD COLUMN group_id TEXT"
            ))
            logger.info("迁移: usage_logs 添加 group_id 列 (Phase 3.A)")
        else:
            logger.debug("迁移: usage_logs.group_id 已存在，跳过")

        # model_group_members 软删除 (P1-2, 2026-08-06)
        # ALTER ADD COLUMN NOT NULL 必须带 DEFAULT (SQLite 限制)
        result = await conn.execute(text("PRAGMA table_info('model_group_members')"))
        columns = {row[1] for row in result.fetchall()}
        if "is_active" not in columns:
            await conn.execute(text(
                "ALTER TABLE model_group_members ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1"
            ))
            logger.info("迁移: model_group_members 添加 is_active 列 (P1-2)")
        else:
            logger.debug("迁移: model_group_members.is_active 已存在，跳过")
        if "removed_at" not in columns:
            await conn.execute(text(
                "ALTER TABLE model_group_members ADD COLUMN removed_at DATETIME"
            ))
            logger.info("迁移: model_group_members 添加 removed_at 列 (P1-2)")
        else:
            logger.debug("迁移: model_group_members.removed_at 已存在，跳过")
        if "removed_reason" not in columns:
            await conn.execute(text(
                "ALTER TABLE model_group_members ADD COLUMN removed_reason TEXT"
            ))
            logger.info("迁移: model_group_members 添加 removed_reason 列 (P1-2)")
        else:
            logger.debug("迁移: model_group_members.removed_reason 已存在，跳过")
        if "removed_by" not in columns:
            await conn.execute(text(
                "ALTER TABLE model_group_members ADD COLUMN removed_by TEXT"
            ))
            logger.info("迁移: model_group_members 添加 removed_by 列 (P1-2)")
        else:
            logger.debug("迁移: model_group_members.removed_by 已存在，跳过")

        # model_feedback_summary (V4.4.A): Base.metadata.create_all 已负责建表和索引
        # 老库也无需 ALTER；表不存在时会在上面的 create_all 创建。
        logger.debug("迁移: model_feedback_summary 由 metadata 幂等创建")

        # accounts.notes (V4.5 fix, 2026-08-08): V4.4 add_account.py CLI 传 notes=args.notes
        # 但 Account ORM 缺列, 导致 create_account 抛 TypeError.
        # 修复: 加列并补 migration.
        result = await conn.execute(text("PRAGMA table_info('accounts')"))
        columns = {row[1] for row in result.fetchall()}
        if "notes" not in columns:
            await conn.execute(text("ALTER TABLE accounts ADD COLUMN notes TEXT"))
            logger.info("迁移: accounts 添加 notes 列 (V4.5 fix)")
        else:
            logger.debug("迁移: accounts.notes 已存在，跳过")


async def drop_all():
    """删除所有表（仅开发/测试用）"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    logger.info("所有表已删除")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(init_db())
    print("数据库初始化成功！")
