"""极简 Dashboard 路由

提供：
- /dashboard -> 单页面 HTML
- /api/dashboard/* -> Dashboard 数据 API
- /api/dashboard/sources/run-now -> 手动触发情报采集 (Phase 2A-4)
"""

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from db.engine import get_session
from db import crud
from db.models import (
    Channel, Promotion, UsageLog, PriceSnapshot,
    ModelRegistry, ModelGroup, ModelGroupMember,
    SettingsRuntime,
)

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


@router.post("/api/sources/run-now")
async def run_source_now(provider: str = Query(..., description="厂商标识，如 deepseek / qwen / glm")):
    """手动触发指定 provider 的情报采集（不等定时器）

    Phase 2A-4：改完代码想立刻验证、刚加完新 source 不想等 15 分钟时用。
    任务在后台跑（fire-and-forget），接口立即返回。

    Returns:
        200: { "status": "started", "provider": "...", "count": N }
        400: 未知 provider
    """
    from intelligence.scheduler import run_source_now as _run_now

    count = await _run_now(provider)
    if count == 0:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {provider}")

    return {
        "status": "started",
        "provider": provider,
        "count": count,
        "note": "task 在后台跑，fire-and-forget",
    }


@router.post("/api/cache/refresh")
async def refresh_route_cache():
    """手动刷新路由缓存 (Phase 3 #5)

    用途: 改完 channel / account / price 后立即生效, 不必等 5 分钟自动刷新.
    场景:
    - 改了 .env 的 key 重新跑 manual_import.py
    - 跑了 refresh_siliconflow_models.py 加了新 model
    - 在 DB 里手动改了 priority
    - API-Sec-2: 手动刷 alerter settings cache (虽然 PUT /api/settings/alert 已自动刷)

    Returns:
        200: { "status": "ok", "channels": N, "models": M }
    """
    from gateway.router import route_engine
    from intelligence.alerter import refresh_settings_cache

    try:
        await route_engine.refresh_cache()
    except Exception as e:
        logger.error(f"路由缓存刷新失败: {e}")
        raise HTTPException(status_code=500, detail=f"Refresh route failed: {e}")

    try:
        await refresh_settings_cache()
    except Exception as e:
        logger.error(f"告警 settings 缓存刷新失败: {e}")
        # 不抛 500, alerter cache 不是关键路径
        # (alerter 自己 fallback .env)

    channels = len(route_engine._channels_cache)
    models = len(route_engine.get_available_models())
    return {
        "status": "ok",
        "channels": channels,
        "models": models,
    }


# ============================================================
# Phase 2.9: Model Registry + Model Group Dashboard (只读)
# ============================================================

@router.get("/api/registry/models")
async def get_registry_models(session: AsyncSession = Depends(get_session)):
    """Phase 2.9: 列出 model_registry (25 个 model 只读)

    跟 model_cards (43 张) 双轨 (B 方案, model_id soft join)
    """
    rows = list((await session.execute(
        select(ModelRegistry).order_by(ModelRegistry.family, ModelRegistry.tier, ModelRegistry.model_id)
    )).scalars().all())
    return {
        "count": len(rows),
        "models": [
            {
                "model_id": r.model_id,
                "family": r.family,
                "display_name": r.display_name,
                "tier": r.tier,
                "capabilities": r.capabilities,
                "context_length": r.context_length,
                "modality": r.modality,
                "is_open_source": bool(r.is_open_source),
                "release_date": r.release_date,
                "status": r.status,
            }
            for r in rows
        ],
    }


@router.get("/api/registry/groups")
async def get_registry_groups(session: AsyncSession = Depends(get_session)):
    """Phase 2.9: 列出 model_group + members (6 group + 35 member 只读)
    """
    groups = list((await session.execute(
        select(ModelGroup).order_by(ModelGroup.sort_order)
    )).scalars().all())
    members = list((await session.execute(
        select(ModelGroupMember)
    )).scalars().all())

    # 倒排 member by group_id
    members_by_group: dict[str, list] = {}
    for m in members:
        members_by_group.setdefault(m.group_id, []).append({
            "model_id": m.model_id,
            "priority": m.priority,
        })

    return {
        "group_count": len(groups),
        "member_count": len(members),
        "groups": [
            {
                "group_id": g.group_id,
                "display_name": g.display_name,
                "tier": g.tier,
                "capabilities": g.capabilities,
                "min_context": g.min_context,
                "strategy": g.strategy,
                "fallback_group": g.fallback_group,
                "enabled": bool(g.enabled),
                "members": members_by_group.get(g.group_id, []),
            }
            for g in groups
        ],
    }


# ============================================================
# API-Sec-2: Runtime Settings (alert_channel / webhook) 不重启切换
# ============================================================

# 白名单: 允许的 runtime setting key
ALLOWED_RUNTIME_KEYS = {
    "alert_channel",
    "bark_key",
    "bark_server",
    "serverchan_key",
    "feishu_webhook_url",
}


@router.get("/api/settings/alert")
async def get_alert_settings(session: AsyncSession = Depends(get_session)):
    """API-Sec-2: 读当前告警 runtime override + .env 默认值

    返回每个 key 的: current (生效值) / override (DB 存的) / env_default (.env 兜底)
    - current 跟 override 一样 -> DB 有 override
    - current 跟 env_default 一样 -> DB 没 override, 用 .env
    """
    from config import settings
    env_defaults = {
        "alert_channel": settings.alert_channel,
        "bark_key": settings.bark_key,
        "bark_server": settings.bark_server,
        "serverchan_key": settings.serverchan_key,
        "feishu_webhook_url": settings.feishu_webhook_url,
    }
    overrides = {r.key: r.value for r in await crud.list_runtime_settings(session)}
    result = {}
    for key in sorted(ALLOWED_RUNTIME_KEYS):
        env_val = env_defaults.get(key, "")
        override_val = overrides.get(key)
        # 简化: key/webhook url 不暴露明文到 GET (避免 log 抓取), 只显示是否 override
        is_secret = key.endswith("_key") or key.endswith("_url")
        if is_secret:
            current_display = "(已配置)" if (override_val or env_val) else "(未配置)"
            override_display = "(已设置)" if override_val else None
            env_display = "(已设置)" if env_val else "(未配置)"
        else:
            current_display = override_val or env_val
            override_display = override_val
            env_display = env_val
        result[key] = {
            "current": current_display,
            "override": override_display,
            "env_default": env_display,
            "is_override": override_val is not None,
        }
    return result


@router.put("/api/settings/alert")
async def set_alert_settings(
    payload: dict,
    session: AsyncSession = Depends(get_session),
):
    """API-Sec-2: 改告警 runtime override, 立即生效 (不重启)

    body 格式:
    {
        "alert_channel": "feishu",            // 设为 feishu
        "bark_key": null,                     // 删 override (回退 .env)
        "feishu_webhook_url": "https://..."   // 设为新值
    }

    只接受白名单字段; value=null 表示删 override.
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be dict")

    # 白名单校验
    invalid = set(payload.keys()) - ALLOWED_RUNTIME_KEYS
    if invalid:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid keys: {sorted(invalid)}. Allowed: {sorted(ALLOWED_RUNTIME_KEYS)}",
        )

    updated = []
    cleared = []
    for key, value in payload.items():
        if value is None:
            await crud.set_runtime_setting(session, key, None)
            cleared.append(key)
        else:
            if not isinstance(value, str):
                raise HTTPException(
                    status_code=400,
                    detail=f"{key} must be string or null, got {type(value).__name__}",
                )
            await crud.set_runtime_setting(session, key, value)
            updated.append(key)

    # 立即刷 alerter cache (不依赖 POST /api/cache/refresh)
    from intelligence.alerter import refresh_settings_cache
    await refresh_settings_cache()

    logger.info(f"API-Sec-2: 更新 runtime settings: +{updated} -{cleared}")
    return {
        "status": "ok",
        "updated": updated,
        "cleared": cleared,
        "note": "立即生效, 不需要重启 uvicorn",
    }
