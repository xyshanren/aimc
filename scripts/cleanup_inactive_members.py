"""P1-2 (2026-08-06) 软删除 row 清理脚本

背景:
- P1-2 把 model_group_members 的"出组"改为软删除 (is_active=0 + 记录审计字段)
- 优点: 出组可恢复 (加回组只需重置 is_active=1 + 清 audit 字段)
- 缺点: 软删除行永远占 DB 空间, 长期累积会膨胀

设计:
- 软删除 30 天后自动清理 (保留 audit trail 1 个月, 类似 GDPR-like 清理)
- 默认 dry_run: 只 print 待清理行数 + 按 reason 分组统计, 不真删
- --apply: 真 DELETE
- 跟 intelligence/scheduler.py 一样, 可手动跑 + 后续挂到 daily scheduler

跑法:
    # 干跑 (推荐, 先看会清多少)
    python scripts/cleanup_inactive_members.py

    # 真删
    python scripts/cleanup_inactive_members.py --apply

    # 自定义阈值 (默认 30 天)
    python scripts/cleanup_inactive_members.py --days 7 --apply
"""

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, func, select, text

sys.path.insert(0, str(Path(__file__).parent.parent))

from db.engine import async_session_factory
from db.models import ModelGroupMember

logger = logging.getLogger(__name__)


# 默认 30 天阈值, 保留 audit trail 1 个月
DEFAULT_RETENTION_DAYS = 30


async def cleanup_inactive_group_members(
    days: int = DEFAULT_RETENTION_DAYS,
    dry_run: bool = True,
) -> dict:
    """清理 is_active=0 且 removed_at < (now - days) 的 ModelGroupMember 行

    Args:
        days: 保留天数 (软删除后多少天清理), 默认 30
        dry_run: True 只统计不删, False 真 DELETE

    Returns:
        dict: {
            "would_delete": int,           # 待删除数 (dry_run 时即实际返回数)
            "deleted": int,                # 实际删除数 (dry_run 时为 0)
            "by_reason": {reason: count},  # 按 removed_reason 分组
            "threshold_iso": str,          # 阈值 ISO 时间
        }
    """
    now = datetime.now()
    threshold = now - timedelta(days=days)
    threshold_iso = threshold.isoformat()

    async with async_session_factory() as session:
        # 0) 防御性 schema check: P1-2 migration 是否运行?
        #    没 P1-2 migration 就没有 is_active 列, 脚本没必要跑
        result = await session.execute(
            text("PRAGMA table_info('model_group_members')")
        )
        columns = {row[1] for row in result.fetchall()}
        if "is_active" not in columns or "removed_at" not in columns:
            logger.warning(
                "model_group_members 缺 P1-2 软删除列 (is_active/removed_at)。\n"
                "请先跑: python db/init_db.py 应用 P1-2 migration。\n"
                "本脚本跳过执行, 返回 0。"
            )
            return {
                "would_delete": 0,
                "deleted": 0,
                "by_reason": {},
                "threshold_iso": threshold_iso,
                "skipped": "schema_not_migrated",
            }

        has_reason_column = "removed_reason" in columns

        # 1) 统计待删除: 按 reason 分组 (如列存在)
        if has_reason_column:
            stmt = (
                select(
                    ModelGroupMember.removed_reason,
                    func.count(ModelGroupMember.id).label("count"),
                )
                .where(
                    ModelGroupMember.is_active == 0,
                    ModelGroupMember.removed_at.isnot(None),
                    ModelGroupMember.removed_at < threshold,
                )
                .group_by(ModelGroupMember.removed_reason)
            )
        else:
            # 列不存在: 只拿总数
            stmt = select(func.count(ModelGroupMember.id)).where(
                ModelGroupMember.is_active == 0,
                ModelGroupMember.removed_at.isnot(None),
                ModelGroupMember.removed_at < threshold,
            )

        rows = (await session.execute(stmt)).all()

        if has_reason_column:
            by_reason = {row.removed_reason or "unknown": row.count for row in rows}
        else:
            total_count = rows[0][0] if rows else 0
            by_reason = {"(no_reason_column)": total_count}

        total = sum(by_reason.values())

        if total == 0:
            logger.info(
                f"无需清理: 0 行 is_active=0 且 removed_at < {threshold_iso} "
                f"(阈值 {days} 天)"
            )
            return {
                "would_delete": 0,
                "deleted": 0,
                "by_reason": {},
                "threshold_iso": threshold_iso,
            }

        # 2) dry_run: 只 print 统计
        if dry_run:
            logger.info(
                f"[DRY RUN] 待清理 {total} 行 (阈值 {days} 天, removed_at < {threshold_iso})"
            )
            for reason, count in sorted(by_reason.items(), key=lambda x: -x[1]):
                logger.info(f"  - {reason}: {count} 行")

            # 抽样列出前 5 条 (group_id / model_id / removed_at / removed_by)
            sample_stmt = (
                select(
                    ModelGroupMember.group_id,
                    ModelGroupMember.model_id,
                    ModelGroupMember.removed_at,
                    ModelGroupMember.removed_by,
                )
                .where(
                    ModelGroupMember.is_active == 0,
                    ModelGroupMember.removed_at.isnot(None),
                    ModelGroupMember.removed_at < threshold,
                )
                .order_by(ModelGroupMember.removed_at)
                .limit(5)
            )
            samples = (await session.execute(sample_stmt)).all()
            logger.info("  抽样 (最早 5 条):")
            for group_id, model_id, removed_at, removed_by in samples:
                logger.info(
                    f"    {removed_at.isoformat()}  group={group_id}  "
                    f"model={model_id}  by={removed_by}"
                )
            logger.info(
                f"\n确认要清理? 加 --apply 标志: "
                f"python scripts/cleanup_inactive_members.py --apply"
            )
            return {
                "would_delete": total,
                "deleted": 0,
                "by_reason": by_reason,
                "threshold_iso": threshold_iso,
            }

        # 3) 真删: DELETE WHERE is_active=0 AND removed_at < threshold
        delete_stmt = (
            delete(ModelGroupMember)
            .where(
                ModelGroupMember.is_active == 0,
                ModelGroupMember.removed_at.isnot(None),
                ModelGroupMember.removed_at < threshold,
            )
        )
        result = await session.execute(delete_stmt)
        await session.commit()

        deleted = result.rowcount or 0
        logger.info(
            f"[APPLIED] 删除 {deleted} 行 (阈值 {days} 天, removed_at < {threshold_iso})"
        )
        for reason, count in sorted(by_reason.items(), key=lambda x: -x[1]):
            logger.info(f"  - {reason}: {count} 行 (已删)")

        return {
            "would_delete": total,
            "deleted": deleted,
            "by_reason": by_reason,
            "threshold_iso": threshold_iso,
        }


async def main():
    parser = argparse.ArgumentParser(
        description="清理 P1-2 软删除的 ModelGroupMember 行 (默认 30 天前)"
    )
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_RETENTION_DAYS,
        help=f"保留天数 (软删除后多少天清理), 默认 {DEFAULT_RETENTION_DAYS}",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真删 (默认 dry_run, 只 print 不删)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    result = await cleanup_inactive_group_members(
        days=args.days,
        dry_run=not args.apply,
    )

    # 汇总输出 (供 shell 抓取)
    if args.apply:
        print(f"\n完成: 实际删除 {result['deleted']} 行 (阈值 {args.days} 天)")
    else:
        print(f"\n[DRY RUN] 待清理 {result['would_delete']} 行 (加 --apply 真删)")


if __name__ == "__main__":
    asyncio.run(main())
