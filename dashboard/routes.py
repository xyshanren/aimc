"""极简 Dashboard 路由

提供：
- /dashboard -> 单页面 HTML
- /api/dashboard/* -> Dashboard 数据 API
- /api/dashboard/sources/run-now -> 手动触发情报采集 (Phase 2A-4)
- /api/dashboard/accounts/* -> V4.6 账号管理 (列表/加/启停/轮换/过期扫描)
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from db.engine import get_session
from db import crud
from db.models import (
    Channel, Account, Promotion, UsageLog, PriceSnapshot,
    ModelRegistry, ModelGroup, ModelGroupMember,
    ModelCapabilitiesLog,
    CrossSourceDiscrepancy,
    SettingsRuntime,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

STATIC_DIR = Path(__file__).parent / "static"
# V4.5: 周报目录, 与 scripts/generate_weekly_report.py 同步
REPORTS_DIR = Path(__file__).parent.parent / "reports"


def _list_weekly_reports() -> list[dict]:
    """列出 reports/ 下所有 weekly_YYYY-MM-DD.md, 按日期倒序。"""
    if not REPORTS_DIR.exists():
        return []
    out: list[dict] = []
    for path in REPORTS_DIR.glob("weekly_*.md"):
        try:
            stat = path.stat()
            out.append({
                "filename": path.name,
                "date": path.stem.replace("weekly_", ""),
                "size_bytes": stat.st_size,
                "modified_at": stat.st_mtime,
            })
        except OSError:
            continue
    out.sort(key=lambda r: r["date"], reverse=True)
    return out


@router.get("", response_class=HTMLResponse)
async def dashboard_page():
    """Dashboard 主页面"""
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return HTMLResponse(content=index_file.read_text(encoding="utf-8"))
    return HTMLResponse(content="<h1>AIMC Dashboard</h1><p>static/index.html not found</p>")


# ============================================================
# P1-7 (2026-08-14): Dashboard 鉴权 (跟 api_key 分开)
# ============================================================
# - AIMC_DASHBOARD_TOKEN 未设 = 无鉴权 (legacy, LAN 内裸开)
# - AIMC_DASHBOARD_TOKEN 设了 = /dashboard/* 需要 token 校验
# - 校验逻辑在 gateway/middleware.py (RequestLoggingMiddleware)
# - 这里只提供 login/status/logout 三个端点 + login.html 静态页


@router.get("/login", response_class=HTMLResponse)
async def dashboard_login_page(next: str = "/dashboard"):
    """P1-7: dashboard 登录页 (浏览器访问 /dashboard 无 token 时重定向到这里)

    显示一个简单的 token 输入框, 提交后 JS 把 token 存 localStorage,
    并跳转到 ?next= 指定的路径
    """
    login_file = STATIC_DIR / "login.html"
    if login_file.exists():
        return HTMLResponse(content=login_file.read_text(encoding="utf-8"))
    # Fallback inline HTML (跟 static/login.html 一致)
    return HTMLResponse(content=_LOGIN_HTML_FALLBACK.format(next_path=next))


# Fallback login HTML (避免引入独立 static 文件 — 简单优先)
_LOGIN_HTML_FALLBACK = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>AIMC Dashboard Login</title>
<style>
body {{ font-family: system-ui; max-width: 400px; margin: 80px auto; padding: 20px; }}
input {{ width: 100%; padding: 10px; margin: 8px 0; box-sizing: border-box; font-size: 14px; }}
button {{ width: 100%; padding: 10px; background: #1f6feb; color: white; border: none; cursor: pointer; font-size: 14px; }}
button:hover {{ background: #1858bf; }}
.err {{ color: #d73a49; margin-top: 10px; font-size: 13px; }}
.hint {{ color: #6a737d; font-size: 12px; margin-top: 16px; }}
</style></head>
<body>
<h2 style="margin-top:0;">AIMC Dashboard</h2>
<p>Enter your dashboard token to continue.</p>
<input id="t" type="password" placeholder="Dashboard token" autofocus>
<button onclick="submit()">Login</button>
<div id="err" class="err"></div>
<div class="hint">Token 来源: AIMC_DASHBOARD_TOKEN 环境变量 / .env</div>
<script>
async function submit() {{
  const token = document.getElementById('t').value.trim();
  if (!token) {{ document.getElementById('err').innerText = 'Token required'; return; }}
  // 验证 + 存 cookie (HttpOnly)
  const resp = await fetch('/dashboard/api/auth/login', {{
    method: 'POST',
    headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify({{ token }})
  }});
  if (resp.ok) {{
    localStorage.setItem('aimc_dashboard_token', token);
    location.href = '{next_path}';
  }} else {{
    document.getElementById('err').innerText = 'Invalid token';
    localStorage.removeItem('aimc_dashboard_token');
  }}
}}
document.getElementById('t').addEventListener('keypress', e => {{ if (e.key === 'Enter') submit(); }});
</script>
</body></html>
"""


@router.get("/api/auth/status")
async def dashboard_auth_status():
    """P1-7: dashboard 鉴权状态查询

    返回:
    - auth_enabled: bool — dashboard 是否启用鉴权 (跟 env AIMC_DASHBOARD_TOKEN 联动)
    - auth_required: bool — 当前请求是否已鉴权 (有有效 token)

    前端 JS 用这个判断:
    - auth_enabled=False → 直接显示 dashboard (legacy)
    - auth_enabled=True + auth_required=False → 跳 /dashboard/login
    - auth_enabled=True + auth_required=True → 显示 dashboard
    """
    from config import settings
    return {
        "auth_enabled": settings.dashboard_auth_enabled,
        "auth_required": False,  # middleware 已拦截未鉴权请求, 走到这里都是有 token 的
    }


@router.post("/api/auth/login")
async def dashboard_auth_login(request: Request):
    """P1-7: dashboard 登录端点 (备选 — 浏览器可用 fetch 提交而非 URL query)

    Body: {"token": "<token>"}
    Returns:
    - 200 + Set-Cookie: aimc_dashboard_token=<token>; HttpOnly (浏览器场景)
    - 401: 错误 token
    """
    from starlette.responses import JSONResponse
    from config import settings
    import hmac

    body = await request.json()
    provided = body.get("token", "").strip()
    if not hmac.compare_digest(provided, settings.dashboard_token):
        return JSONResponse(
            status_code=401,
            content={"error": {"message": "Invalid token", "type": "authentication_error"}},
        )
    # 设 cookie (浏览器场景)
    response = JSONResponse(content={"ok": True})
    response.set_cookie(
        key="aimc_dashboard_token",
        value=provided,
        httponly=True,
        samesite="lax",
        max_age=86400 * 7,  # 7 天
    )
    return response


@router.post("/api/auth/logout")
async def dashboard_auth_logout():
    """P1-7: dashboard 登出 (清 cookie)"""
    from starlette.responses import JSONResponse
    response = JSONResponse(content={"ok": True})
    response.delete_cookie("aimc_dashboard_token")
    return response


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

    P1-2: 仅显示 is_active=1 的成员. 出组的 model 仍在表里 (审计),
    但 UI/路由不出现, 避免误导运维/路由命中 deprecated 模型.
    """
    groups = list((await session.execute(
        select(ModelGroup).order_by(ModelGroup.sort_order)
    )).scalars().all())
    members = list((await session.execute(
        select(ModelGroupMember).where(ModelGroupMember.is_active == 1)
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
# Phase 3.A: Group 路由命中率聚合 (Dashboard 唯一路径)
# ============================================================

@router.get("/api/stats/group_routing")
async def get_group_routing_stats(days: int = 7, session: AsyncSession = Depends(get_session)):
    """Phase 3.A: 按 group_id 聚合命中率分布

    背景:
    - Agent 改 model="tier:balanced" 后, AIMC 会按价格/可用性自动选最优具体 model
    - 用户/人想看: 7 天内 tier:balanced 实际命中了哪些 model (deepseek/qwen/...)?
    - 数据源: usage_logs.group_id (NULL 表示精确模型路由)

    返回:
    - groups[]: 每个 group 的请求数 + 实际 model 分布 + 渠道分布
    - unmatched: 精确模型路由 (group_id NULL) 的请求
    - summary: 总览数字
    """
    from datetime import datetime, timedelta
    cutoff = datetime.utcnow() - timedelta(days=days)

    # 1) 按 group_id + model 聚合 (只统计 group_id 非 NULL)
    stmt = (
        select(
            UsageLog.group_id,
            UsageLog.model,
            UsageLog.channel_id,
            func.count(UsageLog.id).label("count"),
            func.sum(UsageLog.cost_estimate).label("total_cost"),
        )
        .where(UsageLog.created_at >= cutoff)
        .where(UsageLog.group_id.isnot(None))
        .group_by(UsageLog.group_id, UsageLog.model, UsageLog.channel_id)
    )
    rows = (await session.execute(stmt)).all()

    # 2) 反向 join channel name
    ch_ids = {row.channel_id for row in rows if row.channel_id is not None}
    ch_name_map: dict[int, str] = {}
    if ch_ids:
        ch_rows = (await session.execute(
            select(Channel.id, Channel.name).where(Channel.id.in_(ch_ids))
        )).all()
        ch_name_map = {r.id: r.name for r in ch_rows}

    # 3) 按 group_id 聚合
    by_group: dict[str, dict] = {}
    for row in rows:
        g = by_group.setdefault(row.group_id, {
            "group_id": row.group_id,
            "request_count": 0,
            "model_distribution": {},
            "channels_used": set(),
            "total_cost": 0.0,
        })
        g["request_count"] += row.count
        g["model_distribution"][row.model] = g["model_distribution"].get(row.model, 0) + row.count
        if row.channel_id and row.channel_id in ch_name_map:
            g["channels_used"].add(ch_name_map[row.channel_id])
        g["total_cost"] += (row.total_cost or 0.0)

    # 4) 转 list + 算 percentage
    groups_out = []
    for g in by_group.values():
        total = g["request_count"]
        dist = sorted(
            [
                {
                    "model": m,
                    "count": c,
                    "percentage": round(c / total * 100, 1) if total else 0.0,
                }
                for m, c in g["model_distribution"].items()
            ],
            key=lambda x: -x["count"],
        )
        groups_out.append({
            "group_id": g["group_id"],
            "request_count": total,
            "model_distribution": dist,
            "channels_used": sorted(g["channels_used"]),
            "total_cost": round(g["total_cost"], 4),
        })
    groups_out.sort(key=lambda x: -x["request_count"])

    # 5) unmatched: group_id 为 NULL 的精确模型路由
    stmt2 = (
        select(
            UsageLog.model,
            func.count(UsageLog.id).label("count"),
        )
        .where(UsageLog.created_at >= cutoff)
        .where(UsageLog.group_id.is_(None))
        .where(UsageLog.model.isnot(None))
        .group_by(UsageLog.model)
        .order_by(func.count(UsageLog.id).desc())
        .limit(20)
    )
    unmatched_rows = (await session.execute(stmt2)).all()
    unmatched_total = sum(r.count for r in unmatched_rows)

    # 6) 总览 summary
    total_requests = sum(g["request_count"] for g in groups_out) + unmatched_total

    return {
        "days": days,
        "cutoff": cutoff.isoformat(),
        "summary": {
            "total_requests": total_requests,
            "group_routed_requests": sum(g["request_count"] for g in groups_out),
            "exact_model_requests": unmatched_total,
            "group_routed_pct": round(
                sum(g["request_count"] for g in groups_out) / total_requests * 100, 1
            ) if total_requests else 0.0,
        },
        "groups": groups_out,
        "unmatched": {
            "request_count": unmatched_total,
            "top_models": [
                {"model": r.model, "count": r.count} for r in unmatched_rows
            ],
        },
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
    "price_alert_threshold",  # Phase 4 E2: 价格波动告警阈值 (百分比字符串)
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


# ============================================================
# Phase 4 E1: Dashboard 待确认队列 (model_capabilities_log.approved=0)
# ============================================================

@router.get("/api/pending-review")
async def get_pending_review(
    limit: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
):
    """Phase 4 E1: 列出 AI 推断但尚未人工确认的能力变更。

    数据源: model_capabilities_log WHERE approved=0 AND source='ai_infer'
    按创建时间倒序。

    返回每条记录包含:
    - id, model_id, field_changed, old_value, new_value
    - confidence, created_at
    - 待确认原因 (按 confidence 分级)
    """
    rows = await crud.list_pending_review(session, limit=limit)
    return {
        "count": len(rows),
        "items": [
            {
                "id": r.id,
                "model_id": r.model_id,
                "field_changed": r.field_changed,
                "old_value": r.old_value,
                "new_value": r.new_value,
                "confidence": r.confidence,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "confidence_level": (
                    "high" if (r.confidence or 0) >= 0.8
                    else "medium" if (r.confidence or 0) >= 0.6
                    else "low"
                ),
            }
            for r in rows
        ],
    }


@router.post("/api/pending-review/{log_id}/approve")
async def approve_pending_review(
    log_id: int,
    session: AsyncSession = Depends(get_session),
):
    """人工批准一条能力变更 (Phase 4 E1)。

    行为:
    - log.approved 置 1
    - 若 field_changed == "tier": 同步更新 model_registry.tier + 从旧 tier 组踢出 + 加入新 tier 组
    - 其它字段 (capabilities / context_length / modality / metadata / create) 仅作记录
    - P1-1 fix: commit 后调 route_engine.refresh_cache() 让路由立即拿到新 tier/成员

    返回:
    - 200: { status: "approved", log_id, field_changed, applied, model_id, new_group }
    - 404: log 不存在
    """
    log = await crud.approve_capability_change(session, log_id)
    if not log:
        raise HTTPException(status_code=404, detail=f"Pending review {log_id} not found")
    await session.commit()

    # P1-1 fix: 路由缓存即刻刷新. 失败只 logger 警告, 不让 dashboard 5xx.
    try:
        await route_engine.refresh_cache()
    except Exception as e:
        logger.warning("approve 后刷新路由缓存失败: log_id=%s err=%s", log_id, e)

    applied = log.field_changed == "tier" and log.new_value is not None
    # 若 tier 迁移, 报上新 group_id (供 dashboard UI 反馈)
    new_group = None
    if log.field_changed == "tier" and log.new_value:
        new_group = await crud.find_target_group_for_tier(session, log.new_value)
    return {
        "status": "approved",
        "log_id": log.id,
        "field_changed": log.field_changed,
        "applied": applied,
        "model_id": log.model_id,
        "new_group": new_group,
    }


@router.post("/api/pending-review/{log_id}/reject")
async def reject_pending_review(
    log_id: int,
    session: AsyncSession = Depends(get_session),
):
    """人工拒绝一条能力变更 (Phase 4 E1)。

    行为:
    - log.approved 置 1 (拒绝 = 不再待确认)
    - 不动 model_registry (保留当前值)

    返回:
    - 200: { status: "rejected", log_id }
    - 404: log 不存在
    """
    log = await crud.reject_capability_change(session, log_id)
    if not log:
        raise HTTPException(status_code=404, detail=f"Pending review {log_id} not found")
    await session.commit()
    return {
        "status": "rejected",
        "log_id": log.id,
        "field_changed": log.field_changed,
    }


# ============================================================
# Phase 4 E3: Dashboard 跨源分歧 (cross_source_discrepancies)
# ============================================================

@router.get("/api/cross-source/discrepancies")
async def get_cross_source_discrepancies(
    status: str = Query("pending", pattern="^(pending|resolved|ignored)$"),
    limit: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
):
    """Phase 4 E3: 列出跨源分歧记录。

    Returns 每条:
    - id, model_id, field_changed, sources_json (parsed), discrepancy_score
    - status, resolved_value, resolved_at
    """
    rows = await crud.list_cross_source_discrepancies(session, status=status, limit=limit)
    return {
        "count": len(rows),
        "items": [
            {
                "id": r.id,
                "model_id": r.model_id,
                "field_changed": r.field_changed,
                "sources": json.loads(r.sources_json) if r.sources_json else [],
                "discrepancy_score": r.discrepancy_score,
                "status": r.status,
                "resolved_value": r.resolved_value,
                "resolved_note": r.resolved_note,
                "resolved_at": r.resolved_at.isoformat() if r.resolved_at else None,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rows
        ],
    }


@router.post("/api/cross-source/discrepancies/{discrepancy_id}/resolve")
async def resolve_cross_source_discrepancy(
    discrepancy_id: int,
    payload: dict,
    session: AsyncSession = Depends(get_session),
):
    """Phase 4 E3: 人工拍板跨源分歧。

    body: {
        "resolved_value": "<最终值>" 或 JSON 字符串 for capabilities,
        "resolved_note": "可选备注",
        "ignored": false  # true 时状态为 ignored
    }

    P1-10 fix: 拍板后调 route_engine.refresh_cache() 并回流 model_registry:
    - tier / capabilities / context_length / modality 字段拍板后, 将结果写一条
      model_capabilities_log (source="human_resolve", confidence=1.0, approved=1)
      并同步更新 model_registry 对应字段. 下一个请求路由能拿到新值.
    - ignored=true 时不写 registry (表示误报, 只改 discrepancy 状态).
    - 其它字段 (e.g. benchmark_scores) 仅动 discrepancy, 不写 registry.
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be dict")
    resolved_value = payload.get("resolved_value")
    resolved_note = payload.get("resolved_note")
    ignored = bool(payload.get("ignored", False))
    # P1-12 fix: ignored=true 时允许 resolved_value 为空 (仅作误报标记, 不需拍板值).
    # 非 ignored 才校验 resolved_value 必填, 避免上游调用方误传空值被 400 拒掉.
    if not ignored and resolved_value is None:
        raise HTTPException(status_code=400, detail="resolved_value required")

    row = await crud.resolve_cross_source_discrepancy(
        session,
        discrepancy_id=discrepancy_id,
        resolved_value=resolved_value,
        resolved_note=resolved_note,
        ignored=ignored,
    )
    if not row:
        raise HTTPException(status_code=404, detail=f"Discrepancy {discrepancy_id} not found")

    # P1-10 fix: 人工拍板后, 同步更新 model_registry 字段 (tier/capabilities/context_length/modality)
    applied = False
    if not ignored and row.field_changed in ("tier", "capabilities", "context_length", "modality"):
        from db.models import ModelRegistry

        registry = await session.get(ModelRegistry, row.model_id)
        if registry:
            old_value = getattr(registry, row.field_changed, None)
            # P1-7 fix: 先捕获旧值, 再 mutate, 最后 logger
            registry_updates = {
                "tier": resolved_value,
                "capabilities": resolved_value,
                "context_length": int(resolved_value) if row.field_changed == "context_length" else None,
                "modality": resolved_value,
            }
            new_field_value = registry_updates[row.field_changed]
            if row.field_changed == "context_length" and new_field_value is None:
                logger.warning(
                    "resolve context_length 解析失败: model=%s_value=%s",
                    row.model_id, resolved_value,
                )
            else:
                setattr(registry, row.field_changed, new_field_value)
                # 同步写一条 audit log (source="human_resolve", approved=1, 永久生效)
                await crud.log_capability_change(
                    session,
                    model_id=row.model_id,
                    field_changed=row.field_changed,
                    old_value=str(old_value) if old_value is not None else None,
                    new_value=str(new_field_value),
                    source="human_resolve",
                    confidence=1.0,
                    approved=1,
                )
                logger.info(
                    "resolve 同源: model=%s %s %s -> %s (discrepancy_id=%d)",
                    row.model_id, row.field_changed, old_value, new_field_value, discrepancy_id,
                )
                applied = True
        else:
            logger.warning(
                "resolve 时 registry 缺失: model=%s (discrepancy_id=%d)",
                row.model_id, discrepancy_id,
            )

    await session.commit()

    # P1-10 fix: 路由缓存即刻刷新. 失败只 logger 警告, 不让 dashboard 5xx.
    try:
        await route_engine.refresh_cache()
    except Exception as e:
        logger.warning("resolve 后刷新路由缓存失败: discrepancy_id=%s err=%s", discrepancy_id, e)

    return {
        "status": "ignored" if ignored else "resolved",
        "discrepancy_id": row.id,
        "model_id": row.model_id,
        "field_changed": row.field_changed,
        "resolved_value": row.resolved_value,
        "applied": applied,
    }


@router.post("/api/cross-source/run-detection")
async def run_cross_source_detection():
    """手动触发跨源分歧检测 (Phase 4 E3)。

    Returns: { total_models, with_discrepancy, recorded }
    """
    from intelligence.cross_source_validator import detect_and_record
    try:
        summary = await detect_and_record()
    except Exception as e:
        logger.error(f"跨源检测失败: {e}")
        raise HTTPException(status_code=500, detail=f"Detection failed: {e}")
    return summary


# ============================================================
# V4.5: Dashboard 周报查看页 (V4.4.B 生成, V4.5 提供 UI)
# ============================================================

@router.get("/api/reports")
async def list_reports():
    """列出 reports/ 下所有周报 + 指向 latest 的元信息。

    Returns:
        {
            "count": int,
            "latest": {date, filename, mtime, size_bytes} | None,
            "reports": [{date, filename, size_bytes, modified_at}, ...]
        }

    Note: 目录不存在 (未跑过 generate_weekly_report) 时返 count=0, latest=null。
    """
    reports = _list_weekly_reports()
    latest_path = REPORTS_DIR / "weekly-latest.md"
    latest: dict | None = None
    if latest_path.exists():
        stat = latest_path.stat()
        latest = {
            "filename": "weekly-latest.md",
            "size_bytes": stat.st_size,
            "modified_at": stat.st_mtime,
            # latest 的 period_start 从第一个 weekly_*.md 推断
            "date": reports[0]["date"] if reports else None,
        }
    return {
        "count": len(reports),
        "latest": latest,
        "reports": reports,
    }


@router.get("/api/reports/latest")
async def get_latest_report():
    """返回 weekly-latest.md 原文 (Markdown)。)

    用于 Dashboard 渲染。报头 X-AIMC-Meta 携带元数据 (date / size / modified_at)。
    """
    latest_path = REPORTS_DIR / "weekly-latest.md"
    if not latest_path.exists():
        raise HTTPException(
            status_code=404,
            detail="weekly-latest.md 不存在 — 请先跑 python scripts/generate_weekly_report.py",
        )
    text = latest_path.read_text(encoding="utf-8")
    stat = latest_path.stat()
    return HTMLResponse(
        content=f"<pre class='report-md'>{text}</pre>",
        headers={
            "X-AIMC-Size": str(stat.st_size),
            "X-AIMC-Modified": str(int(stat.st_mtime)),
            "Cache-Control": "no-cache",
        },
    )


@router.get("/api/reports/{date}")
async def get_report_by_date(date: str):
    """返回指定日期的周报 Markdown。date 格式 YYYY-MM-DD。"""
    # 防路径穿越 — 只允许 YYYY-MM-DD 格式
    import re as _re
    if not _re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise HTTPException(status_code=400, detail=f"Invalid date format: {date}")
    report_path = REPORTS_DIR / f"weekly_{date}.md"
    if not report_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"周报 weekly_{date}.md 不存在",
        )
    text = report_path.read_text(encoding="utf-8")
    return HTMLResponse(
        content=f"<pre class='report-md'>{text}</pre>",
        headers={"Cache-Control": "no-cache"},
    )


# ============================================================
# V4.6: Dashboard 账号管理 (替代 SSH + 编辑 .env)
# 业务逻辑与 scripts/add_account.py / rotate_account.py / expire_accounts.py
# 对齐, 但走 HTTP, dashboard 一条命令加账号无需重启.
# ============================================================


def _mask_key_display(ciphertext: Optional[bytes]) -> str:
    """账号列表里显示的 key 状态: encrypted(hex 末尾4字节) / missing.

    为什么不解密: dashboard handler 不依赖 AIMC_MASTER_KEY, 避免路由层错误传递.
    实际明文只有加密入库路径 (scripts/add_account.py) 会写入, 无 dashboard 反向解密需求.
    """
    if not ciphertext:
        return "(未加密)"
    # 取末尾 4 字节 hex 作为弱校验标识 (同账号轮换时变化, 但不可逆推明文)
    return f"encrypted:{ciphertext[-4:].hex()}"


def _account_to_dict(account: Account, channel: Channel) -> dict:
    """账号 + channel 转 dict 给前端. key 脱敏, status 颜色由前端 style.css 控制."""
    return {
        "id": account.id,
        "channel_id": account.channel_id,
        "provider": channel.provider,
        "channel_name": channel.name,
        "account_name": account.account_name,
        "status": account.status,
        "quota_total": account.quota_total,
        "quota_used": account.quota_used or 0,
        "quota_remaining": account.quota_remaining,
        "quota_pct": (
            round(account.quota_used / account.quota_total * 100, 1)
            if account.quota_total else None
        ),
        "last_rotated_at": account.last_rotated_at.isoformat() if account.last_rotated_at else None,
        "expires_at": account.expires_at.isoformat() if account.expires_at else None,
        "notes": account.notes,
        "key_display": _mask_key_display(account.api_key_ciphertext),
        "created_at": account.created_at.isoformat() if account.created_at else None,
    }


@router.get("/api/accounts")
async def list_accounts(
    provider: Optional[str] = Query(None, description="过滤 provider, e.g. deepseek"),
    status: Optional[str] = Query(None, description="过滤 status, e.g. active"),
    session: AsyncSession = Depends(get_session),
):
    """V4.6: 列出账号 (脱敏), 供 Dashboard 账号管理页.

    与 scripts/list_accounts.py 一致: 走 crud.list_accounts, 默认按 id 排序.
    """
    rows = await crud.list_accounts(session, provider=provider, status=status)
    return {
        "count": len(rows),
        "accounts": [_account_to_dict(acc, ch) for acc, ch in rows],
    }


@router.get("/api/channels/active")
async def list_active_channels(session: AsyncSession = Depends(get_session)):
    """V4.6: 列出 active channels, 供 Dashboard 加账号表单的 provider 下拉选项."""
    rows = list((await session.execute(
        select(Channel).where(Channel.status == "active").order_by(Channel.provider)
    )).scalars().all())
    return {
        "count": len(rows),
        "channels": [
            {
                "id": ch.id,
                "provider": ch.provider,
                "name": ch.name,
            }
            for ch in rows
        ],
    }


def _parse_iso_datetime(value) -> Optional[datetime]:
    """解析 ISO 8601 datetime/date 字符串; 容错: 'YYYY-MM-DD' 也接受."""
    if not value:
        return None
    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail=f"expires_at must be string, got {type(value).__name__}")
    # datetime.fromisoformat 支持 'YYYY-MM-DD' (Python 3.11+) / 'YYYY-MM-DDTHH:MM:SS'
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        # 试一下仅日期格式
        try:
            return datetime.fromisoformat(value + "T00:00:00")
        except ValueError:
            raise HTTPException(status_code=400, detail=f"expires_at format invalid: {value} (use YYYY-MM-DD or ISO)")


@router.post("/api/accounts")
async def create_account_dashboard(
    payload: dict,
    session: AsyncSession = Depends(get_session),
):
    """V4.6: Dashboard 加账号 (等价 scripts/add_account.py).

    body:
    {
        "provider": "deepseek",       # 必填, active channel
        "name": "deepseek_02",        # 必填, 同 channel 下唯一
        "api_key": "sk-xxx",          # 必填, 不写明文字段, 直接 encrypt 入 ciphertext
        "quota_total": 100000,        # 可选
        "expires_at": "2026-11-08",   # 可选, ISO date 或 datetime
        "notes": "新用户优惠"          # 可选 (V4.5 fix: accounts.notes 列)
    }

    返回:
    - 201: { status: created, account: {...} }
    - 400: 字段缺失 / 类型错
    - 404: provider 无 active channel
    - 409: 同 channel 下 name 重复
    """
    from config import settings
    from security.crypto import encrypt

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be dict")

    provider = payload.get("provider")
    name = payload.get("name")
    api_key = payload.get("api_key")
    quota_total = payload.get("quota_total")
    expires_at_raw = payload.get("expires_at")
    notes = payload.get("notes")

    if not provider or not isinstance(provider, str):
        raise HTTPException(status_code=400, detail="provider required (string)")
    if not name or not isinstance(name, str):
        raise HTTPException(status_code=400, detail="name required (string)")
    if not api_key or not isinstance(api_key, str):
        raise HTTPException(status_code=400, detail="api_key required (string)")
    if not settings.aimc_master_key:
        raise HTTPException(status_code=500, detail="AIMC_MASTER_KEY 未配置, 无法加密 API key")
    if quota_total is not None and not isinstance(quota_total, int):
        raise HTTPException(status_code=400, detail="quota_total must be int or null")

    expires_at = _parse_iso_datetime(expires_at_raw)

    # 1) 找 active channel
    channel = (await session.execute(
        select(Channel).where(Channel.provider == provider, Channel.status == "active").order_by(Channel.id)
    )).scalars().first()
    if not channel:
        raise HTTPException(
            status_code=404,
            detail=f"未找到 active channel: {provider} — 请先在渠道状态页添加或激活",
        )

    # 2) 重名检查
    existing = await crud.list_accounts(session, provider=provider)
    if any(acc.account_name == name for acc, _ in existing):
        raise HTTPException(status_code=409, detail=f"账号已存在: {name} (provider={provider})")

    # 3) 加密入库
    account = await crud.create_account(
        session,
        channel_id=channel.id,
        account_name=name,
        api_key="",
        api_key_ciphertext=encrypt(api_key, settings.aimc_master_key),
        status="active",
        quota_total=quota_total,
        quota_used=0,
        last_rotated_at=datetime.now(),
        expires_at=expires_at,
        notes=notes,
    )
    await session.commit()

    # 4) 路由缓存即刻刷新, 新账号下一请求可命中
    try:
        from gateway.router import route_engine
        await route_engine.refresh_cache()
    except Exception as e:
        logger.warning("账号新增后刷新路由缓存失败: id=%s err=%s", account.id, e)

    logger.info("V4.6 dashboard add: id=%s provider=%s name=%s", account.id, provider, name)
    return {
        "status": "created",
        "account": _account_to_dict(account, channel),
        "note": "下次路由生效已触发 refresh_cache",
    }


async def _set_account_status(session: AsyncSession, account_id: int, new_status: str) -> dict:
    """V4.6: 切换 active/disabled, 返回 dict 或抛 404."""
    account = await crud.update_account_status(session, account_id, new_status)
    if not account:
        raise HTTPException(status_code=404, detail=f"账号不存在: {account_id}")
    await session.commit()
    # refresh 路由缓存
    try:
        from gateway.router import route_engine
        await route_engine.refresh_cache()
    except Exception as e:
        logger.warning("账号 %s 状态变更后刷新路由缓存失败: %s", account_id, e)
    channel = await session.get(Channel, account.channel_id)
    return _account_to_dict(account, channel) if channel else _account_to_dict(account, Channel())


@router.post("/api/accounts/{account_id}/enable")
async def enable_account(account_id: int, session: AsyncSession = Depends(get_session)):
    """V4.6: 启用账号 (等价 rotate_account.py enable --id)."""
    return await _set_account_status(session, account_id, "active")


@router.post("/api/accounts/{account_id}/disable")
async def disable_account(account_id: int, session: AsyncSession = Depends(get_session)):
    """V4.6: 禁用账号 (等价 rotate_account.py disable --id). 软删, 不物理删除."""
    return await _set_account_status(session, account_id, "disabled")


@router.post("/api/accounts/{account_id}/rotate")
async def rotate_account_dashboard(
    account_id: int,
    payload: dict,
    session: AsyncSession = Depends(get_session),
):
    """V4.6: 轮换 key (等价 rotate_account.py rotate --id --new-key).

    body: { "api_key": "sk-new-key-xxx" }

    返回 200/400/404, 同 create.
    """
    from config import settings
    from security.crypto import encrypt

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be dict")
    api_key = payload.get("api_key")
    if not api_key or not isinstance(api_key, str):
        raise HTTPException(status_code=400, detail="api_key required (string)")
    if not settings.aimc_master_key:
        raise HTTPException(status_code=500, detail="AIMC_MASTER_KEY 未配置, 无法加密 API key")

    account = await crud.rotate_account_key(
        session, account_id, encrypt(api_key, settings.aimc_master_key)
    )
    if not account:
        raise HTTPException(status_code=404, detail=f"账号不存在: {account_id}")
    await session.commit()

    try:
        from gateway.router import route_engine
        await route_engine.refresh_cache()
    except Exception as e:
        logger.warning("账号 %s 轮换后刷新路由缓存失败: %s", account_id, e)

    channel = await session.get(Channel, account.channel_id)
    logger.info("V4.6 dashboard rotate: id=%s provider=%s", account_id, channel.provider if channel else "?")
    return {
        "status": "rotated",
        "account": _account_to_dict(account, channel) if channel else _account_to_dict(account, Channel()),
        "note": "下次路由生效已触发 refresh_cache",
    }


@router.post("/api/accounts/expire-check")
async def expire_check_dashboard():
    """V4.6: 扫过期 (等价 rotate_account.py expire-check / scripts/expire_accounts.py).

    返回: { status: ok, expired_count: N }
    """
    from scripts.expire_accounts import expire_accounts
    try:
        count = await expire_accounts()
    except Exception as e:
        logger.error("V4.6 expire-check 失败: %s", e)
        raise HTTPException(status_code=500, detail=f"expire-check failed: {e}")
    if count > 0:
        try:
            from gateway.router import route_engine
            await route_engine.refresh_cache()
        except Exception as e:
            logger.warning("expire-check 后刷新路由缓存失败: %s", e)
    logger.info("V4.6 dashboard expire-check: %d 个账号标记 expired", count)
    return {"status": "ok", "expired_count": count}
