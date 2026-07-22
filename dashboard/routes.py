"""极简 Dashboard 路由

提供：
- /dashboard -> 单页面 HTML
- /api/dashboard/* -> Dashboard 数据 API
"""

import logging
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from db.engine import get_session
from db.models import Channel, Promotion, UsageLog, PriceSnapshot

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

STATIC_DIR = Path(__file__).parent / "static"


@router.get("", response_class=HTMLResponse)
async def dashboard_page():
    """Dashboard 主页面"""
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return HTMLResponse(content=index_file.read_text(encoding="utf-8"))
    return HTMLResponse(content="<h1>AIMC Dashboard</h1><p>static/index.html not found</p>")


@router.get("/api/channels")
async def get_channels_status(session: AsyncSession = Depends(get_session)):
    """渠道状态列表"""
    stmt = select(Channel).order_by(Channel.provider)
    result = await session.execute(stmt)
    channels = result.scalars().all()

    return [
        {
            "id": ch.id,
            "name": ch.name,
            "provider": ch.provider,
            "status": ch.status,
            "priority": ch.priority,
            "accounts": len(ch.accounts),
            "active_accounts": len([a for a in ch.accounts if a.status == "active"]),
        }
        for ch in channels
    ]


@router.get("/api/promotions")
async def get_promotions(status: str = "active", session: AsyncSession = Depends(get_session)):
    """优惠活动列表"""
    stmt = (
        select(Promotion)
        .where(Promotion.status == status)
        .order_by(Promotion.created_at.desc())
        .limit(50)
    )
    result = await session.execute(stmt)
    promotions = result.scalars().all()

    return [
        {
            "id": p.id,
            "provider": p.provider,
            "title": p.title,
            "discount_desc": p.discount_desc,
            "end_date": p.end_date,
            "status": p.status,
            "confidence": p.confidence,
            "url": p.url,
        }
        for p in promotions
    ]


@router.get("/api/usage/summary")
async def get_usage_summary(days: int = 7, session: AsyncSession = Depends(get_session)):
    """用量统计摘要"""
    stmt = (
        select(
            func.date(UsageLog.created_at).label("date"),
            func.count(UsageLog.id).label("request_count"),
            func.sum(UsageLog.input_tokens).label("total_input_tokens"),
            func.sum(UsageLog.output_tokens).label("total_output_tokens"),
            func.avg(UsageLog.latency_ms).label("avg_latency_ms"),
        )
        .group_by(func.date(UsageLog.created_at))
        .order_by(func.date(UsageLog.created_at).desc())
        .limit(days)
    )
    result = await session.execute(stmt)
    rows = result.all()

    return [
        {
            "date": row.date,
            "request_count": row.request_count,
            "total_input_tokens": row.total_input_tokens or 0,
            "total_output_tokens": row.total_output_tokens or 0,
            "avg_latency_ms": round(row.avg_latency_ms or 0, 1),
        }
        for row in rows
    ]


@router.get("/api/prices")
async def get_prices(session: AsyncSession = Depends(get_session)):
    """当前价格列表"""
    stmt = (
        select(PriceSnapshot, Channel.name.label("channel_name"), Channel.provider)
        .join(Channel, PriceSnapshot.channel_id == Channel.id)
        .where(Channel.status == "active")
        .order_by(PriceSnapshot.input_price)
    )
    result = await session.execute(stmt)
    rows = result.all()

    return [
        {
            "id": row.PriceSnapshot.id,
            "channel_name": row.channel_name,
            "provider": row.provider,
            "model": row.PriceSnapshot.model,
            "price_type": row.PriceSnapshot.price_type,
            "time_range": row.PriceSnapshot.time_range,
            "input_price": row.PriceSnapshot.input_price,
            "output_price": row.PriceSnapshot.output_price,
        }
        for row in rows
    ]
