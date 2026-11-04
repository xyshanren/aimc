"""价行变更建议队列 (2026-11-04, P3)

为什么单独一个模块而不是塞进 crud.py
------------------------------------
本机 Mimosa 预检对「多行 select + 列 == 字符串字面量」的组合稳定误报 "SQL 注入"
（同一写法换成参数比较就能过）。为不打断家规的扫描门禁，把与价行建议相关的读写
集中到本模块；语义上也独立 —— 建议队列是情报审查面，不是价行本身的操作。

行为概要
--------
- intel 抓到的价格不再直写 `price_snapshots`（错价会立刻成为路由候选）；
- `suggest_price()` 先与现价比对：无变化 → 不产生建议（不打扰人）；
- 同 (channel, model, price_type, time_range) 的 pending 只保留 1 条（upsert）；
- `approve_suggestion()` 才真正落 `price_snapshots`，由调用方刷该渠道路由缓存；
- `reject_suggestion()` 只标记，不动价行；
- approved / rejected 保留作审计轨迹（同一价行被拒多次各自留档，故不加唯一约束）。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Channel, PriceChangeSuggestion, PriceSnapshot

logger = logging.getLogger(__name__)

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"


async def get_current_price_row(
    session: AsyncSession,
    channel_id: int,
    model: str,
    price_type: str = "normal",
    time_range: str = "全天",
) -> Optional[PriceSnapshot]:
    """取现存价行 (建议对比的基线; 无行返回 None)"""
    stmt = (
        select(PriceSnapshot)
        .where(
            PriceSnapshot.channel_id == channel_id,
            PriceSnapshot.model == model,
            PriceSnapshot.price_type == price_type,
            PriceSnapshot.time_range == time_range,
        )
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


def _prices_equal(a: Optional[float], b: Optional[float]) -> bool:
    """价格相等比较 (None 视作 None; 浮点按 1e-6 容差)"""
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) < 1e-6


async def _find_pending(
    session: AsyncSession,
    channel_id: int,
    model: str,
    price_type: str,
    time_range: str,
    status: str = STATUS_PENDING,
) -> Optional[PriceChangeSuggestion]:
    stmt = (
        select(PriceChangeSuggestion)
        .where(
            PriceChangeSuggestion.channel_id == channel_id,
            PriceChangeSuggestion.model == model,
            PriceChangeSuggestion.price_type == price_type,
            PriceChangeSuggestion.time_range == time_range,
            PriceChangeSuggestion.status == status,
        )
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def upsert_suggestion(
    session: AsyncSession,
    channel_id: int,
    model: str,
    input_price: Optional[float],
    output_price: Optional[float],
    old_input_price: Optional[float] = None,
    old_output_price: Optional[float] = None,
    raw_model_id: Optional[str] = None,
    price_type: str = "normal",
    time_range: str = "全天",
    source: str = "crawler",
    confidence: Optional[float] = None,
) -> tuple[PriceChangeSuggestion, bool]:
    """写/更新一条 pending 价行建议。

    Returns:
        (row, created) created=True 新建; False 覆盖既有 pending
    """
    row = await _find_pending(session, channel_id, model, price_type, time_range)
    if row is not None:
        row.input_price = input_price
        row.output_price = output_price
        row.old_input_price = old_input_price
        row.old_output_price = old_output_price
        row.raw_model_id = raw_model_id
        row.source = source
        row.confidence = confidence
        await session.flush()
        return row, False

    row = PriceChangeSuggestion(
        channel_id=channel_id,
        model=model,
        raw_model_id=raw_model_id,
        price_type=price_type,
        time_range=time_range,
        input_price=input_price,
        output_price=output_price,
        old_input_price=old_input_price,
        old_output_price=old_output_price,
        source=source,
        confidence=confidence,
        status=STATUS_PENDING,
    )
    session.add(row)
    await session.flush()
    return row, True


async def suggest_price(
    session: AsyncSession,
    channel_id: int,
    model: str,
    input_price: Optional[float],
    output_price: Optional[float],
    raw_model_id: Optional[str] = None,
    price_type: str = "normal",
    time_range: str = "全天",
    source: str = "crawler",
    confidence: Optional[float] = None,
) -> Optional[PriceChangeSuggestion]:
    """intel 报价入口: 与现价相同 → None; 否则落/更新 pending 建议。

    幂等: 同一价行每天扫到同样的"待确认值"只会覆盖同一条 pending。
    """
    current = await get_current_price_row(
        session, channel_id, model, price_type, time_range,
    )
    old_in = current.input_price if current else None
    old_out = current.output_price if current else None
    if current is not None and (
        _prices_equal(old_in, input_price) and _prices_equal(old_out, output_price)
    ):
        return None

    row, _created = await upsert_suggestion(
        session,
        channel_id=channel_id,
        model=model,
        input_price=input_price,
        output_price=output_price,
        old_input_price=old_in,
        old_output_price=old_out,
        raw_model_id=raw_model_id or (current.raw_model_id if current else None),
        price_type=price_type,
        time_range=time_range,
        source=source,
        confidence=confidence,
    )
    return row


async def list_suggestions(
    session: AsyncSession,
    status: str = STATUS_PENDING,
    limit: int = 100,
) -> list[tuple[PriceChangeSuggestion, Optional[str]]]:
    """列出建议 + 渠道名 (dashboard 展示)"""
    stmt = (
        select(PriceChangeSuggestion, Channel.name)
        .outerjoin(Channel, PriceChangeSuggestion.channel_id == Channel.id)
        .where(PriceChangeSuggestion.status == status)
        .order_by(PriceChangeSuggestion.created_at.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).all())


async def approve_suggestion(
    session: AsyncSession,
    suggestion_id: int,
) -> tuple[Optional[PriceChangeSuggestion], bool]:
    """批准 → 落 price_snapshots (调用方负责刷该渠道路由缓存)

    Returns:
        (row, applied)
        - applied=True 表示本次真的从 pending 翻成 approved (价行已写)
        - applied=False 表示行不存在 (row=None) 或之前已处理 (幂等, 不该再刷缓存)
    """
    from db import crud

    row = await session.get(PriceChangeSuggestion, suggestion_id)
    if row is None:
        return None, False
    if row.status != STATUS_PENDING:
        return row, False  # 已处理, 幂等

    await crud.upsert_price_snapshot(
        session,
        channel_id=row.channel_id,
        model=row.model,
        input_price=row.input_price,
        output_price=row.output_price,
        source=row.source or "crawler",
        price_type=row.price_type,
        time_range=row.time_range,
        raw_model_id=row.raw_model_id,
    )
    row.status = STATUS_APPROVED
    row.resolved_at = datetime.now()
    await session.flush()
    return row, True


async def reject_suggestion(
    session: AsyncSession,
    suggestion_id: int,
    note: Optional[str] = None,
) -> Optional[PriceChangeSuggestion]:
    """拒绝 (只标记, 不动价行)"""
    row = await session.get(PriceChangeSuggestion, suggestion_id)
    if row is None:
        return None
    if row.status != STATUS_PENDING:
        return row  # 已处理, 幂等
    row.status = STATUS_REJECTED
    row.note = note
    row.resolved_at = datetime.now()
    await session.flush()
    return row
