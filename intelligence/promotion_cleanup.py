"""Phase 3 体验 (2026-09-05): 优惠情报过期清理

策略 (用户确认保留期 30 天):
1. 状态刷新: end_date 已过 (可解析为日期) 的 active/upcoming 记录 → status='expired'
2. 物理删除: status='expired' 且 created_at 超过保留期 (默认 30 天) 的记录

注册: intelligence/scheduler.py 每日 04:20 跑 cleanup_expired_promotions_task
(模块级函数, APScheduler SQLAlchemyJobStore 要求回调可 pickle, 不能用 lambda)
"""

import logging
from datetime import date, datetime, timedelta

logger = logging.getLogger(__name__)

# 保留期天数 (天): env AIMC_PROMOTION_RETENTION_DAYS 可覆盖 (经 settings)
PROMOTION_RETENTION_DAYS = 30

# end_date 是自由文本列, 按常见格式尽力解析; 解析失败不参与过期判定
_END_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S", "%Y年%m月%d日")


def _parse_end_date(text):
    """解析 end_date 文本 → date; 空/解析失败 → None"""
    if not text:
        return None
    raw = str(text).strip()
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        pass
    for fmt in _END_DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


async def cleanup_expired_promotions(retention_days: int = PROMOTION_RETENTION_DAYS) -> dict:
    """过期状态刷新 + 超保留期物理删除 (一个事务)"""
    from sqlalchemy import delete, select, update

    from db.engine import async_session_factory
    from db.models import Promotion

    now = datetime.now()
    cutoff = now - timedelta(days=retention_days)

    async with async_session_factory() as session:
        rows = (await session.execute(
            select(Promotion.id, Promotion.end_date)
            .where(Promotion.status != "expired")
        )).all()
        expired_ids = [
            r.id for r in rows
            if (d := _parse_end_date(r.end_date)) is not None and d < now.date()
        ]
        if expired_ids:
            await session.execute(
                update(Promotion)
                .where(Promotion.id.in_(expired_ids))
                .values(status="expired", updated_at=now)
            )

        del_result = await session.execute(
            delete(Promotion).where(
                Promotion.status == "expired",
                Promotion.created_at < cutoff,
            )
        )
        await session.commit()
        deleted = del_result.rowcount or 0

    if expired_ids or deleted:
        logger.info("优惠情报清理: %d 条标记过期, %d 条删除 (保留 %d 天)",
                    len(expired_ids), deleted, retention_days)
    return {"marked_expired": len(expired_ids), "deleted": deleted}


async def cleanup_expired_promotions_task() -> dict:
    """模块级 task wrapper (供 APScheduler 注册, 不能用 lambda)"""
    try:
        from config import settings

        retention = int(getattr(settings, "promotion_retention_days",
                                PROMOTION_RETENTION_DAYS))
    except Exception:
        retention = PROMOTION_RETENTION_DAYS
    return await cleanup_expired_promotions(retention_days=retention)
