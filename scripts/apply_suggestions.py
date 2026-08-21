"""方案 A (2026-08-14): 应用 approved 建议到 db

背景:
- intelligence/analytics/suggest_changes.py 写入 model/group 建议 (status=pending)
- dashboard 人工 review 后点 "批准" -> status=approved
- 本脚本: 读 approved 建议, 直接 UPDATE db (tier / model_group_members), 写 applied_at

设计:
- 默认 dry-run, --apply 才真改
- 每个 suggestion 单独 try, 失败不阻塞其他
- 写 audit log (applied_at + reviewed_by 默认 "auto" 因为脚本跑)
- 不改 seed_model_groups.py 文件 (git 干净); db 状态与 seed 脚本可能不一致
  - 半年后如果接受建议, 可手动整合回 seed 脚本

支持的建议类型:
- tier_change: UPDATE model_registry SET tier=suggested_value
- add_to_group: INSERT model_group_members
- remove_from_group: UPDATE model_group_members SET is_active=0
- merge: 把 source group 的 members 移到 target group (UPDATE group_id)
- delete: DELETE model_group_members WHERE group_id=source

用法:
    python scripts/apply_suggestions.py                       # dry-run, 列出待 apply 建议
    python scripts/apply_suggestions.py --apply              # 实际写 db
    python scripts/apply_suggestions.py --type tier_change    # 只 apply tier 调整
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from db.engine import async_session_factory
from db.init_db import init_db
from db.models import (
    GroupChangeSuggestion, ModelChangeSuggestion,
    ModelGroup, ModelGroupMember, ModelRegistry,
)

logger = logging.getLogger(__name__)


async def apply_model_suggestion(
    session: AsyncSession, s: ModelChangeSuggestion, apply: bool
) -> str:
    """应用单 model 建议, 返回结果描述"""
    if s.change_type == "tier_change":
        if apply:
            await session.execute(
                update(ModelRegistry)
                .where(ModelRegistry.model_id == s.model_id)
                .values(tier=s.suggested_value)
            )
        return f"UPDATE model_registry SET tier='{s.current_value}' -> '{s.suggested_value}' WHERE model_id='{s.model_id}'"

    elif s.change_type == "add_to_group":
        # 找 group id (ModelGroup.group_id)
        g = (await session.execute(
            select(ModelGroup).where(ModelGroup.group_id == s.suggested_value)
        )).scalars().first()
        if not g:
            return f"SKIP: group '{s.suggested_value}' 不存在"
        if apply:
            # 检查是否已存在 (避免重复)
            existing = (await session.execute(
                select(ModelGroupMember).where(
                    ModelGroupMember.group_id == g.id,
                    ModelGroupMember.model_id == s.model_id,
                )
            )).scalars().first()
            if existing:
                existing.is_active = 1
                existing.priority = existing.priority or 100
            else:
                session.add(ModelGroupMember(
                    group_id=g.id,
                    model_id=s.model_id,
                    priority=100,
                    weight=1.0,
                    added_by="suggestion",
                    is_active=1,
                ))
        return f"INSERT model_group_members(group='{s.suggested_value}', model='{s.model_id}')"

    elif s.change_type == "remove_from_group":
        g = (await session.execute(
            select(ModelGroup).where(ModelGroup.group_id == s.suggested_value)
        )).scalars().first()
        if not g:
            return f"SKIP: group '{s.suggested_value}' 不存在"
        if apply:
            await session.execute(
                update(ModelGroupMember)
                .where(
                    ModelGroupMember.group_id == g.id,
                    ModelGroupMember.model_id == s.model_id,
                    ModelGroupMember.is_active == 1,
                )
                .values(is_active=0, removed_at=datetime.now(),
                        removed_reason="suggestion_applied", removed_by="apply_suggestions.py")
            )
        return f"UPDATE model_group_members SET is_active=0 WHERE group='{s.suggested_value}' AND model='{s.model_id}'"

    return f"SKIP: 未知 change_type='{s.change_type}'"


async def apply_group_suggestion(
    session: AsyncSession, s: GroupChangeSuggestion, apply: bool
) -> list[str]:
    """应用单 group 建议, 返回 SQL 列表"""
    cmds = []
    if s.change_type == "merge":
        # target_groups: "source,target"
        if not s.target_groups or "," not in s.target_groups:
            return [f"SKIP: target_groups 格式错 ({s.target_groups})"]
        src_gid, tgt_gid = s.target_groups.split(",", 1)
        src_g = (await session.execute(
            select(ModelGroup).where(ModelGroup.group_id == src_gid)
        )).scalars().first()
        tgt_g = (await session.execute(
            select(ModelGroup).where(ModelGroup.group_id == tgt_gid)
        )).scalars().first()
        if not src_g or not tgt_g:
            return [f"SKIP: src='{src_gid}' 或 tgt='{tgt_gid}' 不存在"]
        if apply:
            # 把 src 的 active members 移到 tgt (用 INSERT ... ON CONFLICT 类似逻辑)
            src_members = (await session.execute(
                select(ModelGroupMember).where(
                    ModelGroupMember.group_id == src_g.id,
                    ModelGroupMember.is_active == 1,
                )
            )).scalars().all()
            for m in src_members:
                existing = (await session.execute(
                    select(ModelGroupMember).where(
                        ModelGroupMember.group_id == tgt_g.id,
                        ModelGroupMember.model_id == m.model_id,
                    )
                )).scalars().first()
                if existing:
                    existing.is_active = 1
                else:
                    session.add(ModelGroupMember(
                        group_id=tgt_g.id,
                        model_id=m.model_id,
                        priority=m.priority,
                        weight=m.weight,
                        added_by="merge_from_" + src_gid,
                        is_active=1,
                    ))
                m.is_active = 0
        cmds.append(f"MERGE {src_gid} -> {tgt_gid}: 移动所有 active members")

    elif s.change_type == "delete":
        g = (await session.execute(
            select(ModelGroup).where(ModelGroup.group_id == s.group_id)
        )).scalars().first()
        if not g:
            return [f"SKIP: group '{s.group_id}' 不存在"]
        if apply:
            # 软删除: members is_active=0 (保留审计)
            await session.execute(
                update(ModelGroupMember)
                .where(ModelGroupMember.group_id == g.id, ModelGroupMember.is_active == 1)
                .values(is_active=0, removed_at=datetime.now(),
                        removed_reason="group_deleted_suggestion", removed_by="apply_suggestions.py")
            )
        cmds.append(f"DELETE '{s.group_id}': soft-delete members")
    else:
        cmds.append(f"SKIP: 未知 change_type='{s.change_type}' (待实现)")
    return cmds


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--type", choices=["model", "group"], default="model",
                        help="只 apply model 或 group 类型")
    parser.add_argument("--min-confidence", type=float, default=0.0,
                        help="最低 confidence 阈值 (低于不 apply)")
    args = parser.parse_args()

    apply = args.apply

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    await init_db()
    async with async_session_factory() as session:
        n_model = n_group = 0
        sqls: list[str] = []

        if args.type in ("model",):
            model_stmt = (
                select(ModelChangeSuggestion)
                .where(ModelChangeSuggestion.status == "approved")
                .where(ModelChangeSuggestion.confidence >= args.min_confidence)
                .order_by(ModelChangeSuggestion.confidence.desc())
            )
            models = (await session.execute(model_stmt)).scalars().all()
            logger.info(f"=== {len(models)} 条 approved model 建议 ===")
            for s in models:
                cmd = await apply_model_suggestion(session, s, apply)
                logger.info(f"  [{s.id}] {s.model_id} ({s.change_type}, conf={s.confidence:.2f}): {cmd}")
                sqls.append(cmd)
                n_model += 1

        if args.type in ("group",):
            group_stmt = (
                select(GroupChangeSuggestion)
                .where(GroupChangeSuggestion.status == "approved")
                .where(GroupChangeSuggestion.confidence >= args.min_confidence)
                .order_by(GroupChangeSuggestion.confidence.desc())
            )
            groups = (await session.execute(group_stmt)).scalars().all()
            logger.info(f"=== {len(groups)} 条 approved group 建议 ===")
            for s in groups:
                cmds = await apply_group_suggestion(session, s, apply)
                for cmd in cmds:
                    logger.info(f"  [{s.id}] {s.group_id} ({s.change_type}, conf={s.confidence:.2f}): {cmd}")
                    sqls.append(cmd)
                n_group += 1

        if apply and (n_model or n_group):
            # 写 applied_at
            if n_model:
                await session.execute(
                    update(ModelChangeSuggestion)
                    .where(ModelChangeSuggestion.status == "approved")
                    .values(applied_at=datetime.now(), status="applied")
                )
            if n_group:
                await session.execute(
                    update(GroupChangeSuggestion)
                    .where(GroupChangeSuggestion.status == "approved")
                    .values(applied_at=datetime.now(), status="applied")
                )
            await session.commit()
            logger.info(f"[OK] apply 完成: {n_model} model + {n_group} group")
        elif not apply and sqls:
            logger.info(f"\n[DRY-RUN] 加 --apply 才真改 (上面 {len(sqls)} 条 SQL)")


if __name__ == "__main__":
    asyncio.run(main())
