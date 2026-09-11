"""极简 Dashboard 路由

提供：
- /dashboard -> 单页面 HTML
- /api/dashboard/* -> Dashboard 数据 API
- /api/dashboard/sources/run-now -> 手动触发情报采集 (Phase 2A-4)
- /api/dashboard/accounts/* -> V4.6 账号管理 (列表/加/启停/轮换/过期扫描)
"""

import json
import logging
import asyncio
from datetime import datetime, timedelta
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
    RequestTranscript,
    SettingsRuntime,
    CooldownEvent,
    ModelChangeSuggestion, GroupChangeSuggestion,
    User, ApiKey,
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
async def dashboard_login_page():
    """P1-7: dashboard 登录页 (浏览器访问 /dashboard 无 token 时重定向到这里)

    显示一个简单的 token 输入框, 提交后 JS 把 token 存 localStorage,
    并跳转到 ?next= 指定的路径。
    V6.1 安全修复 (Mimosa medium): 服务端不再把 ?next= 拼进页面
    （旧写法 `location.href = '{next_path}'` 构成 服务端污点→JS 注入/开放跳转），
    改由页面 JS 从 URLSearchParams 自取 + 白名单校验 (见 safeNext)。
    """
    login_file = STATIC_DIR / "login.html"
    if login_file.exists():
        return HTMLResponse(content=login_file.read_text(encoding="utf-8"))
    # Fallback inline HTML (避免引入独立 static 文件 — 简单优先)
    return HTMLResponse(content=_LOGIN_HTML_FALLBACK)


# Fallback login HTML (避免引入独立 static 文件 — 简单优先)
# V6.1: 无任何服务端插值; ?next= 由前端 safeNext() 白名单校验后使用
_LOGIN_HTML_FALLBACK = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>AIMC Dashboard Login</title>
<style>
body { font-family: system-ui; max-width: 400px; margin: 80px auto; padding: 20px; }
input { width: 100%; padding: 10px; margin: 8px 0; box-sizing: border-box; font-size: 14px; }
button { width: 100%; padding: 10px; background: #1f6feb; color: white; border: none; cursor: pointer; font-size: 14px; }
button:hover { background: #1858bf; }
.err { color: #d73a49; margin-top: 10px; font-size: 13px; }
.hint { color: #6a737d; font-size: 12px; margin-top: 16px; }
</style></head>
<body>
<h2 style="margin-top:0;">AIMC Dashboard</h2>
<p>Enter your dashboard token to continue.</p>
<input id="t" type="password" placeholder="Dashboard token" autofocus>
<button onclick="submit()">Login</button>
<div id="err" class="err"></div>
<div class="hint">Token 来源: AIMC_DASHBOARD_TOKEN 环境变量 / .env</div>
<script>
// V6.1 安全修复: next 白名单 — 仅站内绝对路径 (拒 //外链/引号/反斜杠/尖括号),
// 与后端此前的 _safe_next_path 规则一致; 不合法回默认 /dashboard
function safeNext() {
  let n = new URLSearchParams(location.search).get('next') || '/dashboard';
  if (!n.startsWith('/') || n.startsWith('//') || n.startsWith('/\\\\')) return '/dashboard';
  if (['"', "'", '<', '>', '\\\\'].some(c => n.includes(c))) return '/dashboard';
  return n;
}
async function submit() {
  const token = document.getElementById('t').value.trim();
  if (!token) { document.getElementById('err').innerText = 'Token required'; return; }
  // 验证 + 存 cookie (HttpOnly)
  const resp = await fetch('/dashboard/api/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token })
  });
  if (resp.ok) {
    localStorage.setItem('aimc_dashboard_token', token);
    location.href = safeNext();
  } else {
    document.getElementById('err').innerText = 'Invalid token';
    localStorage.removeItem('aimc_dashboard_token');
  }
}
document.getElementById('t').addEventListener('keypress', e => { if (e.key === 'Enter') submit(); });
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
    """渠道状态列表 (P1-12: 加 api_base / api_style / supports_prompt_cache)"""
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
            "base_url": ch.base_url,
            "api_base": ch.api_base,  # P1-12: 实际 base_url (覆盖 db.base_url)
            "api_style": ch.api_style or "openai",  # P1-12: openai / anthropic
            "supports_prompt_cache": ch.supports_prompt_cache,  # P1-12: 0/1
            "drop_params": ch.drop_params,  # P2-B: 需剥离的顶层参数 (JSON list / 逗号分隔)
            "auto_drop_params": ch.auto_drop_params,  # V4.8.7: 上游 400 自动学习
            # V6.x: 思考参数剥离开关 (1=请求侧剥离 thinking 字段, 默认开)
            "disable_thinking_pass_through": ch.disable_thinking_pass_through,
            # Phase B (2026-09-11): 思考注入开关 (1=proxy 注入 enable_thinking:false, 默认关)
            "disable_thinking_inject": ch.disable_thinking_inject,
            "accounts": len(ch.accounts),
            "active_accounts": len([a for a in ch.accounts if a.status == "active"]),
        }
        for ch in channels
    ]


@router.put("/api/channels/{channel_id}")
async def update_channel(channel_id: int, request: Request,
                        session: AsyncSession = Depends(get_session)):
    """P1-12 (2026-08-14): 编辑 channel 字段 (api_base / api_style / supports_prompt_cache / base_url / status / priority)

    Body (JSON, 全部可选):
    {
        "base_url": "...",                  # 默认 base_url
        "api_base": "...",                  # 实际 base_url (覆盖 base_url)
        "api_style": "openai" | "anthropic",
        "supports_prompt_cache": 0 | 1,
        "status": "active" | "disabled",
        "priority": 0,
        "drop_params": "reasoning_effort,max_tokens"  # P2-B: 逗号分隔或 JSON list
    }

    改完后调用 route_engine.invalidate_channel(channel_id) 让 router cache 立即刷新
    """
    from gateway.router import route_engine

    body = await request.json()

    ch = (await session.execute(
        select(Channel).where(Channel.id == channel_id)
    )).scalars().first()
    if not ch:
        raise HTTPException(status_code=404, detail=f"Channel {channel_id} not found")

    # 记录原值（用于 dashboard 显示 diff）
    before = {
        "base_url": ch.base_url,
        "api_base": ch.api_base,
        "api_style": ch.api_style,
        "supports_prompt_cache": ch.supports_prompt_cache,
        "status": ch.status,
        "priority": ch.priority,
        "drop_params": ch.drop_params,
        "auto_drop_params": ch.auto_drop_params,
        "disable_thinking_pass_through": ch.disable_thinking_pass_through,
        "disable_thinking_inject": ch.disable_thinking_inject,
    }

    # 更新字段
    allowed_fields = {"base_url", "api_base", "api_style", "supports_prompt_cache",
                      "status", "priority", "drop_params", "auto_drop_params",
                      "disable_thinking_pass_through", "disable_thinking_inject"}
    for field, value in body.items():
        if field in allowed_fields:
            setattr(ch, field, value)
    await session.commit()

    # P1-12 + P0-4: 触发 router cache 立即刷新 (不等 TTL)
    try:
        await route_engine.invalidate_channel(channel_id)
        logger.info(f"P1-12: channel {ch.name} ({channel_id}) 更新 + cache invalidated")
    except Exception as e:
        logger.warning(f"P1-12: invalidate_channel failed (non-fatal): {e}")

    after = {
        "base_url": ch.base_url,
        "api_base": ch.api_base,
        "api_style": ch.api_style,
        "supports_prompt_cache": ch.supports_prompt_cache,
        "status": ch.status,
        "priority": ch.priority,
        "drop_params": ch.drop_params,
        "auto_drop_params": ch.auto_drop_params,
        "disable_thinking_pass_through": ch.disable_thinking_pass_through,
        "disable_thinking_inject": ch.disable_thinking_inject,
    }
    return {"ok": True, "before": before, "after": after}


@router.get("/api/promotions")
async def get_promotions(
    status: str = "active",
    include_flagged: bool = False,
    session: AsyncSession = Depends(get_session),
):
    """优惠活动列表

    P2 fix (2026-08-20): 默认过滤 flagged=1 条目 (AI 解析不确定的垃圾数据),
    只展示可信的优惠情报。?include_flagged=true 查看全部 (调试用)。

    去重: 按 (provider, title) GROUP BY 取最新一条, 避免历史重复入库淹没列表。
    """
    # 子查询: 每个 (provider, title) 取最大 id (即最新插入的那条)
    subq = (
        select(
            Promotion.provider,
            Promotion.title,
            func.max(Promotion.id).label("max_id"),
        )
        .where(Promotion.status == status)
        .group_by(Promotion.provider, Promotion.title)
        .subquery()
    )
    stmt = select(Promotion).join(subq, Promotion.id == subq.c.max_id)
    if not include_flagged:
        stmt = stmt.where(Promotion.flagged == 0)
    stmt = stmt.order_by(Promotion.created_at.desc()).limit(50)
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
            "created_at": p.created_at.isoformat() if p.created_at else None,
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
            func.sum(UsageLog.reasoning_tokens).label("total_reasoning_tokens"),
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
            # V6.x: 思考 token (关思考评估观察期指标, NULL 行不参与 SUM)
            "total_reasoning_tokens": row.total_reasoning_tokens or 0,
            "avg_latency_ms": round(row.avg_latency_ms or 0, 1),
        }
        for row in rows
    ]


# ============================================================
# V6.0 Phase 2 (2026-09-03): 成本审计 — 标价双轨
# docs/next-major-v6-plan.md §2.3 Phase B
#
# 口径:
# - cost_yuan = sum(cost_estimate) 实付 (按渠道 price_snapshots 计费, 免费/订阅=0)
# - list_cost_yuan = sum(list_cost_estimate) 官方标价 (按 registry list_*_price 估算)
# - saved_yuan = list_cost_yuan - cost_yuan_priced, 其中 cost_yuan_priced 只统计
#   标价已知 (list_cost_estimate IS NOT NULL) 的行 — 避免"标价未知"行把节省算成负数
# - list_known_requests / request_count = 标价覆盖率; 覆盖率低时节省数字偏保守
#
# 实现注记 (2026-09-03 Mimosa 拦截记录): execute() 第一参数必须在调用点
# 内联构造 (session.execute(select(...)...) 直写), 不经 stmt 变量中转;
# "标价已知行的实付" 用单列二趟查询, 不用 case() 条件聚合
# ============================================================

def _cost_audit_row_dict(row, cost_yuan_priced: float) -> dict:
    """聚合行 → 响应 dict (统一字段与精度)"""
    list_cost = float(row.list_cost_yuan or 0)
    return {
        "request_count": int(row.request_count),
        "input_tokens": int(row.input_tokens or 0),
        "output_tokens": int(row.output_tokens or 0),
        "cost_yuan": round(float(row.cost_yuan or 0), 4),
        "list_cost_yuan": round(list_cost, 4),
        "saved_yuan": round(list_cost - cost_yuan_priced, 4),
        "list_known_requests": int(row.list_known_requests or 0),
    }


def _cost_audit_totals(items: list[dict]) -> dict:
    """items 汇总 + 标价覆盖率"""
    total_req = sum(i["request_count"] for i in items)
    known = sum(i["list_known_requests"] for i in items)
    return {
        "request_count": total_req,
        "cost_yuan": round(sum(i["cost_yuan"] for i in items), 4),
        "list_cost_yuan": round(sum(i["list_cost_yuan"] for i in items), 4),
        "saved_yuan": round(sum(i["saved_yuan"] for i in items), 4),
        "list_known_requests": known,
        "list_coverage_pct": round(known / total_req * 100, 1) if total_req else 0.0,
    }


@router.get("/api/usage/cost-by-model")
async def get_cost_by_model(
    days: int = Query(7, ge=1, le=90),
    session: AsyncSession = Depends(get_session),
):
    """V6.0 Phase 2: 按模型拆成本 (标价 vs 实付 vs 节省), 按标价降序"""
    cutoff = datetime.now() - timedelta(days=days)
    rows = (
        await session.execute(
            select(
                UsageLog.model,
                func.count(UsageLog.id).label("request_count"),
                func.coalesce(func.sum(UsageLog.input_tokens), 0).label("input_tokens"),
                func.coalesce(func.sum(UsageLog.output_tokens), 0).label("output_tokens"),
                func.coalesce(func.sum(UsageLog.cost_estimate), 0).label("cost_yuan"),
                func.coalesce(func.sum(UsageLog.list_cost_estimate), 0).label("list_cost_yuan"),
                func.count(UsageLog.list_cost_estimate).label("list_known_requests"),
            )
            .where(UsageLog.created_at >= cutoff)
            .group_by(UsageLog.model)
            .order_by(func.coalesce(func.sum(UsageLog.list_cost_estimate), 0).desc())
        )
    ).all()

    # 二趟: 标价已知行的实付合计 (keyed by model)
    priced = {
        r.model: float(r.cost_yuan_priced or 0)
        for r in (
            await session.execute(
                select(
                    UsageLog.model,
                    func.coalesce(func.sum(UsageLog.cost_estimate), 0).label("cost_yuan_priced"),
                )
                .where(UsageLog.created_at >= cutoff)
                .where(UsageLog.list_cost_estimate.isnot(None))
                .group_by(UsageLog.model)
            )
        ).all()
    }

    items = []
    for row in rows:
        item = {"model": row.model or "(未知模型)"}
        item.update(_cost_audit_row_dict(row, priced.get(row.model, 0.0)))
        items.append(item)
    return {"days": days, "totals": _cost_audit_totals(items), "items": items}


@router.get("/api/usage/cost-by-group")
async def get_cost_by_group(
    days: int = Query(7, ge=1, le=90),
    session: AsyncSession = Depends(get_session),
):
    """V6.0 Phase 2: 按请求组拆成本; NULL group_id = 精确模型路由"""
    cutoff = datetime.now() - timedelta(days=days)
    rows = (
        await session.execute(
            select(
                UsageLog.group_id,
                func.count(UsageLog.id).label("request_count"),
                func.coalesce(func.sum(UsageLog.input_tokens), 0).label("input_tokens"),
                func.coalesce(func.sum(UsageLog.output_tokens), 0).label("output_tokens"),
                func.coalesce(func.sum(UsageLog.cost_estimate), 0).label("cost_yuan"),
                func.coalesce(func.sum(UsageLog.list_cost_estimate), 0).label("list_cost_yuan"),
                func.count(UsageLog.list_cost_estimate).label("list_known_requests"),
            )
            .where(UsageLog.created_at >= cutoff)
            .group_by(UsageLog.group_id)
            .order_by(func.coalesce(func.sum(UsageLog.list_cost_estimate), 0).desc())
        )
    ).all()

    priced = {
        r.group_id: float(r.cost_yuan_priced or 0)
        for r in (
            await session.execute(
                select(
                    UsageLog.group_id,
                    func.coalesce(func.sum(UsageLog.cost_estimate), 0).label("cost_yuan_priced"),
                )
                .where(UsageLog.created_at >= cutoff)
                .where(UsageLog.list_cost_estimate.isnot(None))
                .group_by(UsageLog.group_id)
            )
        ).all()
    }

    items = []
    for row in rows:
        item = {"group_id": row.group_id or "(精确路由)"}
        item.update(_cost_audit_row_dict(row, priced.get(row.group_id, 0.0)))
        items.append(item)
    return {"days": days, "totals": _cost_audit_totals(items), "items": items}


@router.get("/api/usage/cost-by-user")
async def get_cost_by_user(
    days: int = Query(7, ge=1, le=90),
    session: AsyncSession = Depends(get_session),
):
    """V6.0 Phase 2: 按调用方拆成本; NULL caller_id = 未鉴权/legacy"""
    cutoff = datetime.now() - timedelta(days=days)
    rows = (
        await session.execute(
            select(
                UsageLog.caller_id,
                User.username,
                User.display_name,
                func.count(UsageLog.id).label("request_count"),
                func.coalesce(func.sum(UsageLog.input_tokens), 0).label("input_tokens"),
                func.coalesce(func.sum(UsageLog.output_tokens), 0).label("output_tokens"),
                func.coalesce(func.sum(UsageLog.cost_estimate), 0).label("cost_yuan"),
                func.coalesce(func.sum(UsageLog.list_cost_estimate), 0).label("list_cost_yuan"),
                func.count(UsageLog.list_cost_estimate).label("list_known_requests"),
            )
            .outerjoin(User, UsageLog.caller_id == User.id)
            .where(UsageLog.created_at >= cutoff)
            .group_by(UsageLog.caller_id, User.username, User.display_name)
            .order_by(func.coalesce(func.sum(UsageLog.list_cost_estimate), 0).desc())
        )
    ).all()

    priced = {
        r.caller_id: float(r.cost_yuan_priced or 0)
        for r in (
            await session.execute(
                select(
                    UsageLog.caller_id,
                    func.coalesce(func.sum(UsageLog.cost_estimate), 0).label("cost_yuan_priced"),
                )
                .where(UsageLog.created_at >= cutoff)
                .where(UsageLog.list_cost_estimate.isnot(None))
                .group_by(UsageLog.caller_id)
            )
        ).all()
    }

    items = []
    for row in rows:
        item = {
            "caller_id": row.caller_id,
            "username": row.username or "(未鉴权/legacy)",
            "display_name": row.display_name,
        }
        item.update(_cost_audit_row_dict(row, priced.get(row.caller_id, 0.0)))
        items.append(item)
    return {"days": days, "totals": _cost_audit_totals(items), "items": items}


@router.get("/api/usage/cost-trend")
async def get_cost_trend(
    days: int = Query(7, ge=1, le=90),
    session: AsyncSession = Depends(get_session),
):
    """V6.0 Phase 2: 按天的成本趋势 (标价 vs 实付 vs 节省), 时间升序 (画图用)"""
    cutoff = datetime.now() - timedelta(days=days)
    rows = (
        await session.execute(
            select(
                func.date(UsageLog.created_at).label("date"),
                func.count(UsageLog.id).label("request_count"),
                func.coalesce(func.sum(UsageLog.input_tokens), 0).label("input_tokens"),
                func.coalesce(func.sum(UsageLog.output_tokens), 0).label("output_tokens"),
                func.coalesce(func.sum(UsageLog.cost_estimate), 0).label("cost_yuan"),
                func.coalesce(func.sum(UsageLog.list_cost_estimate), 0).label("list_cost_yuan"),
                func.count(UsageLog.list_cost_estimate).label("list_known_requests"),
            )
            .where(UsageLog.created_at >= cutoff)
            .group_by(func.date(UsageLog.created_at))
            .order_by(func.date(UsageLog.created_at).asc())
        )
    ).all()

    priced = {
        r.date: float(r.cost_yuan_priced or 0)
        for r in (
            await session.execute(
                select(
                    func.date(UsageLog.created_at).label("date"),
                    func.coalesce(func.sum(UsageLog.cost_estimate), 0).label("cost_yuan_priced"),
                )
                .where(UsageLog.created_at >= cutoff)
                .where(UsageLog.list_cost_estimate.isnot(None))
                .group_by(func.date(UsageLog.created_at))
            )
        ).all()
    }

    items = []
    for row in rows:
        item = {"date": row.date}
        item.update(_cost_audit_row_dict(row, priced.get(row.date, 0.0)))
        items.append(item)
    return {"days": days, "totals": _cost_audit_totals(items), "items": items}


def _approx_channel_saved(channel_id: int, cached_t: int, cache_price_map: dict) -> float:
    """阶段1 (2026-08-14): 按 channel 估算节省金额 (fallback 简化版)

    当前简化: 因为价格表是按 model 维度, 不能精确按 channel 聚合,
    这里用 cache_price_map 平均 cached_input_price * 9 估算节省.

    TODO: 后续改进 — 用 LEFT JOIN price_snapshots WHERE channel_id=X
    按 channel_id 过滤聚合, 给出精确节省
    """
    if cached_t <= 0:
        return 0.0
    if not cache_price_map:
        return cached_t * 0.1 / 1_000_000
    avg_cip = sum(cache_price_map.values()) / len(cache_price_map)
    return cached_t * avg_cip * 9 / 1_000_000


@router.get("/api/data-freshness")
async def get_data_freshness(session: AsyncSession = Depends(get_session)):
    """阶段2 (2026-08-14): APScheduler job 数据新鲜度

    读 job_health 表, 按 last_success_at 倒序, 返回每个 job 的
    last_success / last_error / 距今多久 / total_runs / total_failures.

    用途: dashboard "数据新鲜度" 卡片
    - 显示 "优惠情报 X 小时前更新" (last_success_at)
    - 高亮失败 job (last_error_at 近 + total_failures > 0)
    """
    from datetime import datetime
    from db.models import JobHealth

    now = datetime.now()
    rows = (await session.execute(
        select(JobHealth).order_by(JobHealth.last_success_at.desc().nulls_last())
    )).scalars().all()

    def time_ago(dt: Optional[datetime]) -> Optional[str]:
        if dt is None:
            return None
        delta = (now - dt).total_seconds()
        if delta < 60:
            return f"{int(delta)}秒前"
        if delta < 3600:
            return f"{int(delta // 60)}分钟前"
        if delta < 86400:
            return f"{int(delta // 3600)}小时前"
        return f"{int(delta // 86400)}天前"

    return {
        "now": now.isoformat(),
        "jobs": [
            {
                "job_id": r.job_id,
                "last_run_at": r.last_run_at.isoformat() if r.last_run_at else None,
                "last_run_ago": time_ago(r.last_run_at),
                "last_success_at": r.last_success_at.isoformat() if r.last_success_at else None,
                "last_success_ago": time_ago(r.last_success_at),
                "last_error_at": r.last_error_at.isoformat() if r.last_error_at else None,
                "last_error_ago": time_ago(r.last_error_at),
                "last_error_msg": (r.last_error_msg or "")[:200] if r.last_error_msg else None,
                "last_duration_ms": r.last_duration_ms,
                "total_runs": r.total_runs or 0,
                "total_successes": r.total_successes or 0,
                "total_failures": r.total_failures or 0,
                # 健康度: "healthy" (last_success < 1d) / "warning" (1-3d) / "stale" (>3d) / "unknown" (没成功过)
                "health": (
                    "healthy" if r.last_success_at and (now - r.last_success_at).total_seconds() < 86400
                    else "warning" if r.last_success_at and (now - r.last_success_at).total_seconds() < 3 * 86400
                    else "stale" if r.last_success_at
                    else "unknown"
                ),
            }
            for r in rows
        ],
    }


@router.get("/api/cooldown")
async def get_cooldown_status():
    """V4.8.7 P7: 渠道冷却状态快照 — 让用户看到哪些 account 被冷却了

    返回 cooldown_manager.snapshot() 的增强版, 通过 router cache 反查 channel_name + account_name.
    """
    from gateway.cooldown import cooldown_manager
    from gateway.router import route_engine

    raw = cooldown_manager.snapshot()
    result = []
    for aid, state in raw.items():
        ch_info = None
        for ch in route_engine._channels_cache:
            if ch.get("account_id") == aid:
                ch_info = ch
                break
        result.append({
            "account_id": aid,
            "channel_name": ch_info["name"] if ch_info else "?",
            "account_name": ch_info.get("account_name", "") if ch_info else "",
            **state,
        })
    return result


# === V4.9 (2026-08-25): '🔴 待处理' tab API ===

@router.get("/api/pending-summary")
async def get_pending_summary(
    exclude_transient: bool = Query(False, description="V6.3.1: 顶栏 badge 传 true — 不把瞬时冷却计入 total/items (仍落事件流水)"),
    session: AsyncSession = Depends(get_session),
):
    """V4.9: 聚合 dashboard '🔴 待处理(N)' tab 所需的项

    返回每个 category 的 count + 跳转锚点 + 最新 3 条示例

    V6.3.1 (2026-09-06): 瞬时冷却 (cooldown_degraded) 15~60s 自愈, badge 30s 轮询
    与 ops 60s 轮询共用本端点导致 "badge 有数、进页已空" 的竞态。方案:
    - 顶栏 badge 调用时带 exclude_transient=true, 只数持久项 (needs_human/人工禁用)
    - 无论 exclude 与否, 观察到非零瞬时冷却都会写 cooldown_events 流水 (去重:
      同账号 cooldown_until 与最近一条流水相同则跳过 — 同 episode 不重复落库)
    """
    from gateway.cooldown import cooldown_manager
    from gateway.router import route_engine

    items: list[dict] = []
    total = 0

    # === 1. needs_human = quota_exhausted ===
    quota_aids = [aid for aid, s in cooldown_manager.snapshot().items()
                  if s.get("needs_human") and s.get("last_error_type") == "quota_exhausted"
                  and not s.get("manual_disabled")]
    quota_examples = []
    for aid in quota_aids[:3]:
        s = cooldown_manager.snapshot()[aid]
        ch_info = _lookup_channel_info(route_engine, aid)
        quota_examples.append({
            "account_id": aid,
            "channel": ch_info["name"] if ch_info else "?",
            "account_name": ch_info.get("account_name", "") if ch_info else "",
            "error_msg": s.get("last_error_msg", "")[:120],
            "since": s.get("first_failure_at"),
        })
    if quota_aids:
        items.append({
            "category": "needs_human_quota",
            "count": len(quota_aids),
            "severity": "danger",
            "label": "💰 余额不足",
            "anchor": "accounts-meta",
            "examples": quota_examples,
        })
        total += len(quota_aids)

    # === 2. needs_human = auth_failed ===
    auth_aids = [aid for aid, s in cooldown_manager.snapshot().items()
                 if s.get("needs_human") and s.get("last_error_type") == "auth_failed"
                 and not s.get("manual_disabled")]
    auth_examples = []
    for aid in auth_aids[:3]:
        s = cooldown_manager.snapshot()[aid]
        ch_info = _lookup_channel_info(route_engine, aid)
        auth_examples.append({
            "account_id": aid,
            "channel": ch_info["name"] if ch_info else "?",
            "account_name": ch_info.get("account_name", "") if ch_info else "",
            "error_msg": s.get("last_error_msg", "")[:120],
        })
    if auth_aids:
        items.append({
            "category": "needs_human_auth",
            "count": len(auth_aids),
            "severity": "danger",
            "label": "🔑 Key 失效",
            "anchor": "accounts-meta",
            "examples": auth_examples,
        })
        total += len(auth_aids)

    # === 3. needs_human = model_unsupported ===
    model_aids = [aid for aid, s in cooldown_manager.snapshot().items()
                  if s.get("needs_human") and s.get("last_error_type") == "model_unsupported"
                  and not s.get("manual_disabled")]
    if model_aids:
        examples = []
        for aid in model_aids[:3]:
            s = cooldown_manager.snapshot()[aid]
            ch_info = _lookup_channel_info(route_engine, aid)
            examples.append({
                "account_id": aid,
                "channel": ch_info["name"] if ch_info else "?",
                "error_msg": s.get("last_error_msg", "")[:120],
            })
        items.append({
            "category": "needs_human_model",
            "count": len(model_aids),
            "severity": "warning",
            "label": "🧩 模型不支持",
            "anchor": "accounts-meta",
            "examples": examples,
        })
        total += len(model_aids)

    # === 4. 自动冷却中 (degraded/failing, 非 needs_human) ===
    snap = cooldown_manager.snapshot()
    cooldown_aids = [aid for aid, s in snap.items()
                     if s.get("in_cooldown") and not s.get("needs_human")
                     and not s.get("manual_disabled")]
    if cooldown_aids:
        cooldown_examples = []
        for aid in sorted(cooldown_aids, key=lambda a: snap[a].get("remaining_seconds", 0), reverse=True)[:5]:
            s = snap[aid]
            ch_info = _lookup_channel_info(route_engine, aid)
            err_type = s.get("last_error_type", "")
            remaining = s.get("remaining_seconds", 0)
            msg = s.get("last_error_msg", "")[:100]
            label = f"{err_type} ({remaining:.0f}s)" if err_type else f"冷却中 ({remaining:.0f}s)"
            cooldown_examples.append({
                "account_id": aid,
                "channel": ch_info["name"] if ch_info else "?",
                "account_name": ch_info.get("account_name", "") if ch_info else "",
                "error_msg": f"{label} — {msg}" if msg else label,
            })
        if not exclude_transient:
            items.append({
                "category": "cooldown_degraded",
                "count": len(cooldown_aids),
                "severity": "warning",
                "label": "⚠️ 自动冷却中",
                "anchor": "accounts-meta",
                "examples": cooldown_examples,
            })
            total += len(cooldown_aids)
        # V6.3.1: 抓现场 — 无论 badge 是否排除瞬时项, 都落事件流水
        await _record_cooldown_events(session, snap, cooldown_aids)

    # === 5. 人工禁用 ===
    manual_aids = [aid for aid, s in snap.items()
                   if s.get("manual_disabled")]
    if manual_aids:
        manual_examples = []
        for aid in manual_aids[:3]:
            s = snap[aid]
            ch_info = _lookup_channel_info(route_engine, aid)
            manual_examples.append({
                "account_id": aid,
                "channel": ch_info["name"] if ch_info else "?",
                "account_name": ch_info.get("account_name", "") if ch_info else "",
                "error_msg": "人工禁用",
            })
        items.append({
            "category": "manual_disabled",
            "count": len(manual_aids),
            "severity": "info",
            "label": "🚫 人工禁用",
            "anchor": "accounts-meta",
            "examples": manual_examples,
        })
        total += len(manual_aids)

    return {"total": total, "items": items}


async def _record_cooldown_events(session: AsyncSession, snap: dict, cooldown_aids: list) -> None:
    """V6.3.1: 瞬时冷却抓现场 — 观察 → 去重 → 落 cooldown_events 流水

    去重键: 同账号最近一条流水的 cooldown_until。CooldownManager.mark_failure
    在同一 episode 内不再失败时 cooldown_until 稳定不变 — badge 30s + ops 60s
    双观察者重复调用也只插一行; episode 内再次失败会刷新 cooldown_until,
    产生新行 (可接受: 记录了刷新这个事实)。

    失败安全: 落库出错只打日志, 不影响 pending-summary 主响应。
    """
    from gateway.router import route_engine as _route_engine
    try:
        for aid in cooldown_aids:
            s = snap[aid]
            observed_until = s.get("cooldown_until")
            if observed_until is None:
                continue
            last = (await session.execute(
                select(CooldownEvent.cooldown_until)
                .where(CooldownEvent.account_id == aid)
                .order_by(CooldownEvent.id.desc())
                .limit(1)
            )).scalar()
            if last is not None and abs(last - observed_until) < 0.001:
                continue  # 同一 episode 已记录过
            ch_info = _lookup_channel_info(_route_engine, aid)
            session.add(CooldownEvent(
                account_id=aid,
                channel_name=(ch_info or {}).get("name"),
                account_name=(ch_info or {}).get("account_name"),
                error_type=s.get("last_error_type"),
                error_code=s.get("last_error_code"),
                error_msg=(s.get("last_error_msg") or "")[:200],
                cooldown_until=observed_until,
                remaining_seconds=s.get("remaining_seconds"),
            ))
        await session.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("cooldown_events 落库失败 (不影响 pending-summary): %s", e)
        await session.rollback()


def _lookup_channel_info(route_engine, account_id: int) -> Optional[dict]:
    """通过 account_id 查 channel name + account_name"""
    for ch in route_engine._channels_cache:
        if ch.get("account_id") == account_id:
            return ch
    return None


@router.get("/api/cooldown/needs-human")
async def list_needs_human():
    """V4.9: 列出所有 needs_human=True 的 account (弹窗详细列表用)"""
    from gateway.cooldown import cooldown_manager
    from gateway.router import route_engine

    snap = cooldown_manager.snapshot()
    result = []
    for aid, s in snap.items():
        if not s.get("needs_human"):
            continue
        ch_info = _lookup_channel_info(route_engine, aid)
        result.append({
            "account_id": aid,
            "channel_name": ch_info["name"] if ch_info else "?",
            "account_name": ch_info.get("account_name", "") if ch_info else "",
            "provider": ch_info.get("provider", "") if ch_info else "",
            "last_error_type": s.get("last_error_type"),
            "last_error_code": s.get("last_error_code"),
            "last_error_msg": s.get("last_error_msg"),
            "first_failure_at": s.get("first_failure_at"),
            "count": s.get("count"),
            "manual_disabled": s.get("manual_disabled"),
        })
    return {"count": len(result), "items": result}


@router.post("/api/cooldown/{account_id}/ack-recover")
async def ack_recover(account_id: int, note: str = ""):
    """V4.9: 人工确认账号问题已解决 → mark_success + 立即探活

    Body: {"note": "可选备注"}
    """
    from gateway.cooldown import cooldown_manager
    from db.engine import async_session_factory
    from db.models import Account
    from datetime import datetime
    from sqlalchemy import update

    cooldown_manager.mark_success(account_id)
    # 更新 DB: last_error_at 清掉
    async with async_session_factory() as session:
        await session.execute(
            update(Account)
            .where(Account.id == account_id)
            .values(last_error_at=None, last_error_type=None)
        )
        await session.commit()

    # 立即触发一次探活 (异步, 不阻塞响应)
    asyncio.create_task(_probe_one_now(account_id))

    return {"status": "ok", "account_id": account_id, "note": note}


async def _probe_one_now(account_id: int):
    """立即探活一个 account (供 ack-recover 触发)"""
    from config import settings
    from db.engine import async_session_factory
    from db.models import Account, AccountHealthLog
    from intelligence.channel_health import _probe_one
    from gateway.cooldown import cooldown_manager
    from gateway.error_classifier import ErrorCategory, ErrorInfo
    from sqlalchemy import select

    try:
        async with async_session_factory() as session:
            stmt = select(Account).where(Account.id == account_id)
            acc = (await session.execute(stmt)).scalar_one_or_none()
        if not acc:
            return

        from security.crypto import try_decrypt
        api_key = None
        try:
            if acc.api_key_ciphertext:
                api_key = try_decrypt(acc.api_key_ciphertext, settings.aimc_master_key)
        except Exception:
            pass

        from db.models import Channel
        async with async_session_factory() as session:
            ch = (await session.execute(
                select(Channel).where(Channel.id == acc.channel_id)
            )).scalar_one_or_none()
        if not ch:
            return

        base_url = (ch.api_base or ch.base_url or "").strip()
        result = await _probe_one(account_id, base_url, api_key)

        # 写日志
        async with async_session_factory() as session:
            log = AccountHealthLog(
                account_id=account_id,
                triggered_by="recovery_check",
                success=1 if result["success"] else 0,
                status_code=result.get("status_code"),
                error_type=result.get("error_type"),
                error_msg=result.get("error_msg"),
                latency_ms=result.get("latency_ms"),
            )
            session.add(log)
            await session.commit()

        # 回写 cooldown
        if result["success"]:
            cooldown_manager.probe_result(account_id, success=True)
        else:
            try:
                cat = ErrorCategory(result.get("error_type") or "unknown")
            except ValueError:
                cat = ErrorCategory.UNKNOWN
            info = ErrorInfo(
                category=cat,
                error_code=result.get("error_code"),
                error_msg=result.get("error_msg") or "",
                needs_human=cat in (ErrorCategory.QUOTA_EXHAUSTED, ErrorCategory.AUTH_FAILED,
                                     ErrorCategory.MODEL_UNSUPPORTED),
                suggested_cooldown_seconds=60,
                suggested_probe_after_seconds=300,
            )
            cooldown_manager.probe_result(account_id, success=False, error_info=info)
    except Exception as e:
        import logging as _log
        _log.getLogger(__name__).warning(f"_probe_one_now #{account_id} 失败: {e}")


@router.post("/api/accounts/{account_id}/manual-disable")
async def manual_disable_account(account_id: int):
    """V4.9: 人工禁用 account (路由永久排除)"""
    from gateway.cooldown import cooldown_manager
    from db.engine import async_session_factory
    from db.models import Account
    from datetime import datetime
    from sqlalchemy import update

    cooldown_manager.mark_manual_disabled(account_id)
    async with async_session_factory() as session:
        await session.execute(
            update(Account)
            .where(Account.id == account_id)
            .values(status="manual_disabled", manual_disabled_at=datetime.now())
        )
        await session.commit()
    return {"status": "ok", "account_id": account_id}


@router.post("/api/accounts/{account_id}/manual-enable")
async def manual_enable_account(account_id: int):
    """V4.9: 解除人工禁用"""
    from gateway.cooldown import cooldown_manager
    from db.engine import async_session_factory
    from db.models import Account
    from sqlalchemy import update

    cooldown_manager.clear_manual_disabled(account_id)
    async with async_session_factory() as session:
        await session.execute(
            update(Account)
            .where(Account.id == account_id)
            .values(status="active", manual_disabled_at=None)
        )
        await session.commit()
    return {"status": "ok", "account_id": account_id}


@router.get("/api/cache/stats")
async def get_cache_stats(days: int = 7, session: AsyncSession = Depends(get_session)):
    """阶段1 (2026-08-14): prompt cache 节省成本统计

    聚合最近 N 天 usage_logs 的 cached_input_tokens + price_snapshots.cached_input_price
    计算:
    - 总 input tokens / cache hit tokens / hit rate
    - 按 cache 价 vs 普通价的差额算"节省金额"

    Returns:
        {
            "period_days": 7,
            "total_input_tokens": int,
            "total_cached_tokens": int,
            "hit_rate_pct": float,
            "saved_cost_cny": float,           # 节省金额 (元)
            "by_channel": [
                {"channel_id": int, "channel_name": str,
                 "input_tokens": int, "cached_tokens": int,
                 "hit_rate_pct": float, "saved_cost_cny": float}
            ],
            "by_model": [
                {"model": str, "input_tokens": int, "cached_tokens": int,
                 "hit_rate_pct": float, "saved_cost_cny": float}
            ],
        }
    """
    from datetime import datetime, timedelta

    # 计算时间窗口 (UTC 偏移不考虑, db 存的是本地 time)
    cutoff = datetime.now() - timedelta(days=days)

    # P3 fix (2026-08-21): cached tokens 纳入 Anthropic 风格 cache_read
    # (一行日志要么 OpenAI 风格 cached_input_tokens 要么 Anthropic 风格
    #  cache_read_input_tokens, 相加不会双算; cache_creation 是写缓存成本,
    #  不算"节省"故不纳入)
    cache_tokens_expr = func.coalesce(
        func.coalesce(UsageLog.cached_input_tokens, 0)
        + func.coalesce(UsageLog.cache_read_input_tokens, 0),
        0,
    )
    # 1. 按 channel 聚合 (用 COALESCE 处理 NULL)
    channel_stmt = (
        select(
            UsageLog.channel_id,
            Channel.name.label("channel_name"),
            func.coalesce(func.sum(UsageLog.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(cache_tokens_expr), 0).label("cached_tokens"),
        )
        .join(Channel, UsageLog.channel_id == Channel.id)
        .where(UsageLog.created_at >= cutoff)
        .group_by(UsageLog.channel_id, Channel.name)
        .order_by(func.sum(cache_tokens_expr).desc())
    )
    channel_rows = (await session.execute(channel_stmt)).all()

    # 2. 按 model 聚合
    model_stmt = (
        select(
            UsageLog.model,
            func.coalesce(func.sum(UsageLog.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(cache_tokens_expr), 0).label("cached_tokens"),
        )
        .where(UsageLog.created_at >= cutoff)
        .group_by(UsageLog.model)
        .order_by(func.sum(cache_tokens_expr).desc())
        .limit(10)  # TOP 10 model
    )
    model_rows = (await session.execute(model_stmt)).all()

    # 3. 算节省金额: cached_tokens * (input_price - cached_input_price) / 1e6
    #    从 price_snapshots 读真实 cache 价; input_price 通常 SF 聚合平台未公开
    #    估算: 假设 input_price = cached_input_price * 10 (cache 是 10% off, 节省 90%)
    #    如果有 input_price, 用真实差额
    price_map_stmt = select(
        PriceSnapshot.model,
        PriceSnapshot.input_price,
        PriceSnapshot.cached_input_price,
    )
    price_rows = (await session.execute(price_map_stmt)).all()
    # model -> cached_input_price (元/百万 tokens)
    cache_price_map = {}
    for p in price_rows:
        if p.cached_input_price is None:
            continue
        cache_price_map[p.model] = p.cached_input_price
        # 如果有真实 input_price, 也存 (按 model 维度算差额)
    input_price_map = {}
    for p in price_rows:
        if p.input_price is not None:
            input_price_map[p.model] = p.input_price

    def saved_for(model: str, cached_t: int) -> float:
        """算单个 model 的节省金额 (元)

        算法:
        - 如果有真实 input_price + cached_input_price: 用真实差额
        - 否则用 cached_input_price * 9 (假设 cache 是 10% off, 节省 90%)
        - 完全没价格: 用 0.1 元/百万的兜底
        """
        if cached_t <= 0:
            return 0.0
        cip = cache_price_map.get(model)
        if cip is None:
            return cached_t * 0.1 / 1_000_000  # 兜底
        iip = input_price_map.get(model)
        if iip is not None:
            diff = max(iip - cip, 0)
            return cached_t * diff / 1_000_000
        # 用 cached_price * 9 估算 (cache 省 90%)
        return cached_t * cip * 9 / 1_000_000

    by_channel = []
    for row in channel_rows:
        input_t = int(row.input_tokens or 0)
        cached_t = int(row.cached_tokens or 0)
        hit_rate = (cached_t / input_t * 100) if input_t > 0 else 0.0
        # by_channel 节省金额需要按 channel 算 (channel_id -> models -> 价格), 简化用 model 价
        # 真实算需要 JOIN price_snapshots WHERE channel_id=..., 先用 model 价格近似
        by_channel.append({
            "channel_id": row.channel_id,
            "channel_name": row.channel_name,
            "input_tokens": input_t,
            "cached_tokens": cached_t,
            "hit_rate_pct": round(hit_rate, 1),
            # TODO: 按 channel_id 过滤 price_snapshots 算精确节省
            "saved_cost_cny": round(_approx_channel_saved(row.channel_id, cached_t, cache_price_map), 4),
        })

    by_model = []
    for row in model_rows:
        input_t = int(row.input_tokens or 0)
        cached_t = int(row.cached_tokens or 0)
        hit_rate = (cached_t / input_t * 100) if input_t > 0 else 0.0
        saved = saved_for(row.model, cached_t)
        by_model.append({
            "model": row.model,
            "input_tokens": input_t,
            "cached_tokens": cached_t,
            "hit_rate_pct": round(hit_rate, 1),
            "saved_cost_cny": round(saved, 4),
        })

    # 4. 总计
    total_input = sum(c["input_tokens"] for c in by_channel)
    total_cached = sum(c["cached_tokens"] for c in by_channel)
    total_hit_rate = (total_cached / total_input * 100) if total_input > 0 else 0.0
    total_saved = sum(c["saved_cost_cny"] for c in by_channel)

    return {
        "period_days": days,
        "total_input_tokens": total_input,
        "total_cached_tokens": total_cached,
        "hit_rate_pct": round(total_hit_rate, 1),
        "saved_cost_cny": round(total_saved, 4),
        "by_channel": by_channel,
        "by_model": by_model,
    }


@router.get("/api/suggestions")
async def get_suggestions(
    status: str = "pending",
    suggestion_type: str = "all",  # "model" / "group" / "all"
    session: AsyncSession = Depends(get_session),
):
    """方案 A (2026-08-14): 模型/分组 建议列表

    Query params:
    - status: pending / approved / rejected / applied (default pending)
    - suggestion_type: model / group / all (default all)
    """
    result = {"model": [], "group": []}

    if suggestion_type in ("model", "all"):
        m_stmt = (
            select(ModelChangeSuggestion)
            .where(ModelChangeSuggestion.status == status)
            .order_by(ModelChangeSuggestion.confidence.desc())
        )
        model_rows = (await session.execute(m_stmt)).scalars().all()
        result["model"] = [
            {
                "id": m.id,
                "model_id": m.model_id,
                "change_type": m.change_type,
                "suggested_value": m.suggested_value,
                "current_value": m.current_value,
                "reason": m.reason,
                "confidence": m.confidence,
                "based_on": json.loads(m.based_on) if m.based_on else None,
                "status": m.status,
                "created_at": m.created_at.isoformat() if m.created_at else None,
                "reviewed_at": m.reviewed_at.isoformat() if m.reviewed_at else None,
                "reviewed_by": m.reviewed_by,
                "applied_at": m.applied_at.isoformat() if m.applied_at else None,
            }
            for m in model_rows
        ]

    if suggestion_type in ("group", "all"):
        g_stmt = (
            select(GroupChangeSuggestion)
            .where(GroupChangeSuggestion.status == status)
            .order_by(GroupChangeSuggestion.confidence.desc())
        )
        group_rows = (await session.execute(g_stmt)).scalars().all()
        result["group"] = [
            {
                "id": g.id,
                "group_id": g.group_id,
                "change_type": g.change_type,
                "target_groups": g.target_groups,
                "suggested_config": g.suggested_config,
                "reason": g.reason,
                "confidence": g.confidence,
                "based_on": json.loads(g.based_on) if g.based_on else None,
                "status": g.status,
                "created_at": g.created_at.isoformat() if g.created_at else None,
                "reviewed_at": g.reviewed_at.isoformat() if g.reviewed_at else None,
                "reviewed_by": g.reviewed_by,
                "applied_at": g.applied_at.isoformat() if g.applied_at else None,
            }
            for g in group_rows
        ]

    return result


@router.post("/api/suggestions/{suggestion_type}/{suggestion_id}/approve")
async def approve_suggestion(
    suggestion_type: str,  # "model" / "group"
    suggestion_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """方案 A (2026-08-14): 批准单条建议

    Body: {"reviewed_by": "username"}
    改 status='pending' -> 'approved' (后续 apply_suggestions.py 跑会真正改 db)
    """
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    reviewed_by = body.get("reviewed_by", "dashboard")

    model_cls = ModelChangeSuggestion if suggestion_type == "model" else GroupChangeSuggestion
    s = (await session.execute(
        select(model_cls).where(model_cls.id == suggestion_id)
    )).scalars().first()
    if not s:
        raise HTTPException(status_code=404, detail=f"{suggestion_type} suggestion {suggestion_id} not found")
    if s.status != "pending":
        raise HTTPException(status_code=400, detail=f"建议当前 status='{s.status}', 只能批准 pending")

    s.status = "approved"
    s.reviewed_at = func.now()
    s.reviewed_by = reviewed_by
    await session.commit()
    return {"ok": True, "id": suggestion_id, "status": "approved"}


@router.post("/api/suggestions/{suggestion_type}/{suggestion_id}/reject")
async def reject_suggestion(
    suggestion_type: str,
    suggestion_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """方案 A (2026-08-14): 拒绝单条建议"""
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    reviewed_by = body.get("reviewed_by", "dashboard")

    model_cls = ModelChangeSuggestion if suggestion_type == "model" else GroupChangeSuggestion
    s = (await session.execute(
        select(model_cls).where(model_cls.id == suggestion_id)
    )).scalars().first()
    if not s:
        raise HTTPException(status_code=404, detail=f"{suggestion_type} suggestion {suggestion_id} not found")

    s.status = "rejected"
    s.reviewed_at = func.now()
    s.reviewed_by = reviewed_by
    await session.commit()
    return {"ok": True, "id": suggestion_id, "status": "rejected"}


@router.post("/api/suggestions/apply-all")
async def apply_all_approved_suggestions(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """方案 A (2026-08-14): 触发 apply_suggestions.py (server-side 跑)

    Body: {"min_confidence": 0.5, "reviewed_by": "username"}
    内部: 读 approved 建议, 直接 UPDATE db, 写 applied_at
    """
    from scripts.apply_suggestions import apply_model_suggestion, apply_group_suggestion

    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    min_conf = body.get("min_confidence", 0.0)
    reviewed_by = body.get("reviewed_by", "dashboard_apply")

    n_model = n_group = 0
    errors = []

    m_stmt = (
        select(ModelChangeSuggestion)
        .where(ModelChangeSuggestion.status == "approved")
        .where(ModelChangeSuggestion.confidence >= min_conf)
    )
    for s in (await session.execute(m_stmt)).scalars().all():
        try:
            await apply_model_suggestion(session, s, apply=True)
            s.applied_at = func.now()
            s.status = "applied"
            s.reviewed_by = reviewed_by
            n_model += 1
        except Exception as e:
            errors.append(f"model {s.id}: {e}")

    g_stmt = (
        select(GroupChangeSuggestion)
        .where(GroupChangeSuggestion.status == "approved")
        .where(GroupChangeSuggestion.confidence >= min_conf)
    )
    for s in (await session.execute(g_stmt)).scalars().all():
        try:
            await apply_group_suggestion(session, s, apply=True)
            s.applied_at = func.now()
            s.status = "applied"
            s.reviewed_by = reviewed_by
            n_group += 1
        except Exception as e:
            errors.append(f"group {s.id}: {e}")

    await session.commit()
    return {
        "ok": True,
        "applied_model": n_model,
        "applied_group": n_group,
        "errors": errors,
    }


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


@router.get("/api/sources")
async def list_sources(session: AsyncSession = Depends(get_session)):
    """V4.8.3: 列出所有采集源（合并内存定义 + DB 状态）

    返回每个源的: provider / source_type / url / poll_interval / status /
    last_check_at / error_count / consecutive_no_change / enabled / api_endpoint_configured

    用途: 采集源管理仪表盘展示 + 诊断
    """
    from intelligence.sources import get_all_sources
    from intelligence.sources.base import DEFAULT_POLL_INTERVALS

    mem_sources = get_all_sources()

    # 批量从 DB 读状态 (按 provider+url 索引)
    db_sources_map = {}
    db_result = await session.execute(select(crud.IntelSource))
    for row in db_result.scalars():
        db_sources_map[(row.provider, row.url)] = row

    sources_list = []
    for src in mem_sources:
        db_src = db_sources_map.get((src.provider, src.url))
        interval = src.poll_interval or DEFAULT_POLL_INTERVALS.get(src.source_type, 900)
        sources_list.append({
            "provider": src.provider,
            "source_type": src.source_type,
            "url": src.url or "(API only)",
            "poll_interval": interval,
            "status": db_src.status if db_src else "new",
            "last_check_at": db_src.last_check_at.isoformat() if db_src and db_src.last_check_at else None,
            "error_count": db_src.error_count if db_src else 0,
            "consecutive_no_change": db_src.consecutive_no_change if db_src else 0,
            "enabled": src.enabled and (not db_src or db_src.status != "disabled"),
            "api_endpoint_configured": bool(getattr(src, "api_endpoint", None)),
        })

    # 按 provider 分组排序
    sources_list.sort(key=lambda s: (s["provider"], s["source_type"]))

    return {"count": len(sources_list), "sources": sources_list}


@router.post("/api/sources/{provider}/toggle")
async def toggle_source(
    provider: str,
    session: AsyncSession = Depends(get_session),
):
    """V4.8.3: 启用/禁用某个 provider 的所有采集源

    在 DB 中把 status 切换为 disabled / active。
    scheduler._run_source_task 检查 DB status, disabled 时跳过。
    """
    from intelligence.sources import get_source_by_provider

    sources = get_source_by_provider(provider)
    if not sources:
        raise HTTPException(status_code=404, detail=f"Unknown provider: {provider}")

    toggled = 0
    new_status = None
    for src in sources:
        db_src = await crud.get_intel_source_by_provider_url(session, src.provider, src.url)
        if db_src:
            if db_src.status == "disabled":
                db_src.status = "active"
                new_status = "active"
            else:
                db_src.status = "disabled"
                new_status = "disabled"
            toggled += 1
        else:
            # DB 无行 → upsert 一条并设为 disabled
            await crud.upsert_intel_source(
                session,
                provider=src.provider,
                url=src.url,
                source_type=src.source_type,
                poll_interval=src.poll_interval or 900,
            )
            # 刚 upsert 是 active, 如果要 disable 需要再查一次
            db_src = await crud.get_intel_source_by_provider_url(session, src.provider, src.url)
            if db_src:
                db_src.status = "disabled"
                new_status = "disabled"
                toggled += 1

    await session.commit()
    return {"status": "ok", "provider": provider, "new_status": new_status, "toggled": toggled}


@router.post("/api/sources/{provider}/interval")
async def update_source_interval(
    provider: str,
    interval: int = Query(..., description="新的轮询间隔（秒）", ge=60, le=604800),
    session: AsyncSession = Depends(get_session),
):
    """V4.8.3: 调整某个 provider 所有源的 poll_interval

    写入 DB intel_sources.poll_interval。
    注意: 实际调度间隔在 scheduler.start_scheduler 时从内存 source 定义读取,
    此端点仅更新 DB 记录 (仪表盘展示用 + 下次重启时如读 DB 可生效)。
    要立即生效需重启服务。
    """
    from intelligence.sources import get_source_by_provider

    sources = get_source_by_provider(provider)
    if not sources:
        raise HTTPException(status_code=404, detail=f"Unknown provider: {provider}")

    updated = 0
    for src in sources:
        db_src = await crud.get_intel_source_by_provider_url(session, src.provider, src.url)
        if db_src:
            db_src.poll_interval = interval
            updated += 1
        else:
            await crud.upsert_intel_source(
                session,
                provider=src.provider,
                url=src.url,
                source_type=src.source_type,
                poll_interval=interval,
            )
            updated += 1

    await session.commit()
    return {"status": "ok", "provider": provider, "new_interval": interval, "updated": updated}


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
    # usage_logs.created_at 存本地时间 (P3 修复), cutoff 须用本地 now 匹配
    cutoff = datetime.now() - timedelta(days=days)

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
# Phase 3b (2026-09-05): 请求日志 — usage_logs 明细 + transcript 查看
# docs/dashboard-phase3-design.md §3
# 鉴权: /dashboard/api/* 由 gateway/middleware.py 统一校验
# (dashboard_token 或 admin key); 转录含完整请求体, 仅 admin 可见的
# 设计决议由该中间件保证 — 非 admin key 一律 401。
# ============================================================

@router.get("/api/requests")
async def list_requests(
    days: int = Query(7, ge=1, le=30),
    status: str = Query("all", pattern="^(all|ok|error)$"),
    model: str = Query(None, max_length=100),
    channel_id: int = Query(None, ge=1),
    key_id: int = Query(None, ge=1),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
):
    """请求日志明细 (usage_logs), 状态/model/渠道/key 过滤 + 分页, 时间倒序。

    V6.3 F2: model 支持模糊匹配 (LIKE %model%), 如 model=glm 收窄到 glm 系。
    """
    # usage_logs.created_at 存本地时间 (P3 修复), cutoff 须用本地 now 匹配
    cutoff = datetime.now() - timedelta(days=days)
    conds = [UsageLog.created_at >= cutoff]
    if status == "ok":
        conds.append(UsageLog.status_code < 400)
    elif status == "error":
        conds.append(UsageLog.status_code >= 400)
    if model:
        conds.append(UsageLog.model.like(f"%{model}%"))
    if channel_id:
        conds.append(UsageLog.channel_id == channel_id)
    if key_id:
        conds.append(UsageLog.key_id == key_id)

    base = select(UsageLog).where(*conds)
    total = (await session.execute(
        select(func.count()).select_from(base.subquery())
    )).scalar() or 0

    rows = (await session.execute(
        base.order_by(UsageLog.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )).scalars().all()

    # 反向 join channel name (同 group_routing 惯例)
    ch_ids = {r.channel_id for r in rows if r.channel_id is not None}
    ch_name_map: dict[int, str] = {}
    if ch_ids:
        ch_rows = (await session.execute(
            select(Channel.id, Channel.name).where(Channel.id.in_(ch_ids))
        )).all()
        ch_name_map = {r.id: r.name for r in ch_rows}

    # 反向 join account name (一渠道多账号: 该请求走了哪个上游账号)
    acc_ids = {r.account_id for r in rows if r.account_id is not None}
    acc_name_map: dict[int, str] = {}
    if acc_ids:
        acc_rows = (await session.execute(
            select(Account.id, Account.account_name).where(Account.id.in_(acc_ids))
        )).all()
        acc_name_map = {r.id: r.account_name for r in acc_rows}

    # key 显示名: label (缺 label 用 prefix); key 身份 = 用户 + label 两层,
    # 同名 label 归属不同用户时追加 username 消歧 (key 表很小, 全表查一次)
    key_rows = (await session.execute(
        select(ApiKey.id, ApiKey.label, ApiKey.key_prefix, User.username)
        .join(User, ApiKey.user_id == User.id, isouter=True)
    )).all()
    label_owners: dict = {}
    for r in key_rows:
        base = r.label or r.key_prefix
        if base and r.username:
            label_owners.setdefault(base, set()).add(r.username)
    key_label_map: dict = {}
    for r in key_rows:
        base = r.label or r.key_prefix or (r.username or "")
        if not base:
            continue
        if r.username and len(label_owners.get(base, set())) > 1:
            key_label_map[r.id] = f"{base} ({r.username})"
        else:
            key_label_map[r.id] = base

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "days": days,
        "model": model,
        "channel_id": channel_id,
        "key_id": key_id,
        "items": [
            {
                "id": r.id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "model": r.model,
                "channel_name": ch_name_map.get(r.channel_id),
                "account_name": acc_name_map.get(r.account_id),  # 一渠道多账号: 上游服务账号
                "group_id": r.group_id,
                "key_label": key_label_map.get(r.key_id) if r.key_id else None,
                "status_code": r.status_code,
                "latency_ms": r.latency_ms,
                "input_tokens": r.input_tokens or 0,
                "output_tokens": r.output_tokens or 0,
                "cached_tokens": r.cached_input_tokens or 0,
                "reasoning_tokens": r.reasoning_tokens,  # V6.x: None=上游未返回
                "cost_yuan": r.cost_estimate,
                "list_cost_yuan": r.list_cost_estimate,
                "error_code": r.error_code,
            }
            for r in rows
        ],
    }


# transcript 单体截断保护 (写入侧已有 transcript_log_max_body, 这是读取侧第二道闸)
_TRANSCRIPT_CLIP = 256 * 1024


@router.get("/api/requests/{req_id}/transcript")
async def get_request_transcript(req_id: int, session: AsyncSession = Depends(get_session)):
    """请求转录 (发给上游的 request body + 响应 body), 排障用。

    transcripts 与 usage_logs 无外键关联 (fire-and-forget 写入),
    按 model + 写入时间窗 [-5s, +10min] 最近匹配, best-effort。
    """
    log = (await session.execute(
        select(UsageLog).where(UsageLog.id == req_id)
    )).scalar_one_or_none()
    if log is None:
        raise HTTPException(status_code=404, detail="usage_log not found")

    # 上游服务账号 (一渠道多账号; legacy 行 account_id 为 NULL)
    account_name = None
    if log.account_id is not None:
        account_name = (await session.execute(
            select(Account.account_name).where(Account.id == log.account_id)
        )).scalar_one_or_none()

    if log.created_at is None or not log.model:
        return {
            "usage_log_id": req_id, "matched": False, "truncated": False,
            "request_body": None, "response_body": None, "transcript_created_at": None,
            "account_name": account_name,
        }

    win_start = log.created_at - timedelta(seconds=5)
    win_end = log.created_at + timedelta(minutes=10)
    tr = (await session.execute(
        select(RequestTranscript)
        .where(RequestTranscript.model == log.model)
        .where(RequestTranscript.created_at >= win_start)
        .where(RequestTranscript.created_at <= win_end)
        .order_by(RequestTranscript.created_at.desc())
        .limit(1)
    )).scalar_one_or_none()

    req_body = tr.request_body if tr else None
    resp_body = tr.response_body if tr else None
    truncated = bool(req_body and len(req_body) > _TRANSCRIPT_CLIP) or bool(
        resp_body and len(resp_body) > _TRANSCRIPT_CLIP)
    if req_body and len(req_body) > _TRANSCRIPT_CLIP:
        req_body = req_body[:_TRANSCRIPT_CLIP]
    if resp_body and len(resp_body) > _TRANSCRIPT_CLIP:
        resp_body = resp_body[:_TRANSCRIPT_CLIP]
    return {
        "usage_log_id": req_id,
        "matched": tr is not None,
        "transcript_created_at": tr.created_at.isoformat() if tr and tr.created_at else None,
        "request_body": req_body,
        "response_body": resp_body,
        "truncated": truncated,
        "account_name": account_name,
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
    "router_score_weights",   # P0-7: router score 5 维权重 (JSON)
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


@router.get("/api/settings/router-score-weights")
async def get_router_score_weights(session: AsyncSession = Depends(get_session)):
    """P0-7 (2026-08-14): 读 router score weights

    返回当前生效的 5 维权重:
    - weights: 当前值 (从 settings_runtime 读, 缺则用 router __init__ 默认值)
    - defaults: router __init__ 写死的兜底值
    - sum: 5 个权重之和 (应该接近 1.0)
    """
    import json as _json
    from db.models import SettingsRuntime
    from gateway.router import route_engine

    defaults = {
        "W_PRICE": 0.40, "W_QUOTA": 0.30, "W_SUCCESS": 0.20,
        "W_RECENCY": 0.05, "W_PRIORITY": 0.05,
    }
    row = (await session.execute(
        select(SettingsRuntime).where(SettingsRuntime.key == "router_score_weights")
    )).scalars().first()
    override = None
    if row is not None and row.value is not None:
        try:
            override = _json.loads(row.value)
        except Exception:
            pass
    weights = override if override is not None else route_engine._score_weights
    return {
        "weights": weights,
        "defaults": defaults,
        "override": override,
        "sum": round(sum(weights.values()), 4),
        "description": {
            "W_PRICE": "价格分数权重 (越便宜分越高, 0-1)",
            "W_QUOTA": "配额分数权重 (剩余越多分越高)",
            "W_SUCCESS": "成功率分数权重 (cooldown 跳过, 1h 内 0.5, 1h 外 1.0)",
            "W_RECENCY": "最近路由时间权重 (越久没用越优先)",
            "W_PRIORITY": "渠道优先级权重 (priority/100)",
        },
    }


@router.put("/api/settings/router-score-weights")
async def set_router_score_weights(
    payload: dict,
    session: AsyncSession = Depends(get_session),
):
    """P0-7 (2026-08-14): 改 router score weights

    Body:
    {
        "weights": {
            "W_PRICE": 0.5,    # 价格优先 (省钱)
            "W_QUOTA": 0.2,
            "W_SUCCESS": 0.2,
            "W_RECENCY": 0.05,
            "W_PRIORITY": 0.05
        }
    }

    校验:
    - 必须含 5 个 W_* 字段
    - 每个值在 [0, 1] 范围
    - sum 应接近 1.0 (但不强求)

    写 db 后, router cache 60s 内自动 refresh (P0-4 TTL).
    想立即生效: POST /api/cache/refresh
    """
    import json as _json
    if not isinstance(payload, dict) or "weights" not in payload:
        raise HTTPException(status_code=400, detail="body must be {weights: {...}}")
    weights = payload["weights"]
    if not isinstance(weights, dict):
        raise HTTPException(status_code=400, detail="weights must be dict")

    required = {"W_PRICE", "W_QUOTA", "W_SUCCESS", "W_RECENCY", "W_PRIORITY"}
    missing = required - set(weights.keys())
    extra = set(weights.keys()) - required
    if missing or extra:
        raise HTTPException(
            status_code=400,
            detail=f"weights must have exactly {sorted(required)}, missing={sorted(missing)}, extra={sorted(extra)}",
        )
    for k, v in weights.items():
        if not isinstance(v, (int, float)) or v < 0 or v > 1:
            raise HTTPException(status_code=400, detail=f"{k}={v} must be number in [0, 1]")

    value = _json.dumps(weights, ensure_ascii=False)
    await crud.set_runtime_setting(session, "router_score_weights", value)
    await session.commit()
    return {"ok": True, "weights": weights, "note": "router cache 60s 内 refresh"}


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
    - display_name (来自 model_registry, 方便用户判断)
    - 待确认原因 (按 confidence 分级)
    """
    rows = await crud.list_pending_review(session, limit=limit)
    items = []
    for log, registry in rows:
        items.append({
            "id": log.id,
            "model_id": log.model_id,
            "display_name": registry.display_name if registry else None,
            "field_changed": log.field_changed,
            "old_value": log.old_value,
            "new_value": log.new_value,
            "confidence": log.confidence,
            "provider": log.provider,
            "created_at": log.created_at.isoformat() if log.created_at else None,
            "confidence_level": (
                "high" if (log.confidence or 0) >= 0.8
                else "medium" if (log.confidence or 0) >= 0.6
                else "low"
            ),
        })
    return {
        "count": len(items),
        "items": items,
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
    # V4.8.5 fix: 补局部 import, 否则 NameError 被 except 吞掉, 缓存实际从不刷新
    try:
        from gateway.router import route_engine
        await route_engine.refresh_cache()
    except Exception as e:
        logger.warning("approve 后刷新路由缓存失败: log_id=%s err=%s", log_id, e)

    applied = log.field_changed == "tier" and log.new_value is not None
    # 若 tier 迁移, 报上新 group_id (供 dashboard UI 反馈)
    new_group = None
    if log.field_changed == "tier" and log.new_value:
        new_group = await crud.find_target_group_for_tier(session, log.new_value)
    # V4.8.6: create 批准时检查渠道可用性, 返回 channel_ok 给前端提示
    channel_activated = False
    if log.field_changed == "create":
        channel_activated = await crud.has_available_channel(session, log.model_id)
    return {
        "status": "approved",
        "log_id": log.id,
        "field_changed": log.field_changed,
        "applied": applied,
        "model_id": log.model_id,
        "new_group": new_group,
        "channel_activated": channel_activated,
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
# V6.3 F3 (自动化收口, 2026-09-05): 自动入组数据积累埋点
# 纯只读观察指标, 为 AUTO_JOIN_NON_T0 决策积累数据 (≥2 周后分析 S1-S4)
# ============================================================

def _auto_join_non_t0() -> bool:
    """运行时读取 AUTO_JOIN_NON_T0 (延迟 import 避免循环依赖, 测试可 monkeypatch)"""
    from intelligence.model_sync import AUTO_JOIN_NON_T0
    return AUTO_JOIN_NON_T0


def _auto_join_min_confidence() -> float:
    """运行时读取 AUTO_JOIN_MIN_CONFIDENCE (V6.4 S3, 2026-09-11)"""
    from intelligence.model_sync import AUTO_JOIN_MIN_CONFIDENCE
    return AUTO_JOIN_MIN_CONFIDENCE

@router.get("/api/ops/auto-join-metrics")
async def get_auto_join_metrics(
    days: int = Query(14, ge=1, le=90),
    session: AsyncSession = Depends(get_session),
):
    """V6.3 F3: 自动入组数据积累埋点 (纯只读, 不写任何数据)。

    5 组指标, 服务 AUTO_JOIN_NON_T0 开启决策 (S1-S4):
    - pending_review 积压: model_capabilities_log approved=0 的当前堆积 + 近 N 天每周新增
    - tier 分布: ai_infer 推断结果里 T0-T4 各档占比
    - 审批负担: 近 N 天人工处理量 (2026-09-09 起三态可拆分: 1=批准 / -1=拒绝;
      此前历史行均为 1, 无法回溯拆分, 计入批准数)
    - 组路由命中: usage_logs 按 group_id 计数, tier:xxx = 组路由请求占比, NULL = 精确路由
    - infer_tier 盲区: metadata log 行 new_value JSON 里 inferred_tier=null 的占比
      (parser.py infer_tier 对无法从模型名推断参数规模的返回 None, 这些模型永远
      无法自动入组, 是 AUTO_JOIN 决策的关键盲区)
    """
    # 本端点查两张表, 时间语义不同:
    # - model_capabilities_log.created_at = func.now() (UTC)
    # - usage_logs.created_at = Python 本地时间 (P3 修复)
    cutoff_utc = datetime.utcnow() - timedelta(days=days)
    cutoff_local = datetime.now() - timedelta(days=days)

    # --- 1) pending_review 积压 (当前 + 每周新增) ---
    pending_total = (
        await session.execute(
            select(func.count(ModelCapabilitiesLog.id))
            .where(ModelCapabilitiesLog.approved == 0)
            .where(ModelCapabilitiesLog.source == "ai_infer")
        )
    ).scalar() or 0

    now_utc = datetime.utcnow()
    week_buckets = {
        "0-7d": (now_utc - timedelta(days=7), now_utc),
        "7-14d": (now_utc - timedelta(days=14), now_utc - timedelta(days=7)),
        "14d+": (datetime(2000, 1, 1), now_utc - timedelta(days=14)),
    }
    pending_weekly = []
    if days >= 7:
        for label, (start, end) in week_buckets.items():
            if label == "7-14d" and days < 14:
                continue
            cnt = (
                await session.execute(
                    select(func.count(ModelCapabilitiesLog.id))
                    .where(ModelCapabilitiesLog.approved == 0)
                    .where(ModelCapabilitiesLog.source == "ai_infer")
                    .where(ModelCapabilitiesLog.created_at >= start)
                    .where(ModelCapabilitiesLog.created_at < end)
                )
            ).scalar() or 0
            pending_weekly.append({"bucket": label, "count": cnt})

    # --- 2) tier 分布 (ai_infer 行, new_value 是 'T0'-'T4' 的 tier 行) ---
    tier_rows = (
        await session.execute(
            select(ModelCapabilitiesLog.new_value, func.count(ModelCapabilitiesLog.id))
            .where(ModelCapabilitiesLog.source == "ai_infer")
            .where(ModelCapabilitiesLog.field_changed == "tier")
            .group_by(ModelCapabilitiesLog.new_value)
        )
    ).all()
    tier_distribution = {row[0] or "(null)": row[1] for row in tier_rows}

    # --- 3) 审批负担 (处理量; 2026-09-09 起三态可拆分: 1=批准 / -1=拒绝) ---
    reviewed_count = (
        await session.execute(
            select(func.count(ModelCapabilitiesLog.id))
            .where(ModelCapabilitiesLog.source == "ai_infer")
            .where(ModelCapabilitiesLog.approved != 0)
            .where(ModelCapabilitiesLog.created_at >= cutoff_utc)
        )
    ).scalar() or 0
    approved_count = (
        await session.execute(
            select(func.count(ModelCapabilitiesLog.id))
            .where(ModelCapabilitiesLog.source == "ai_infer")
            .where(ModelCapabilitiesLog.approved == 1)
            .where(ModelCapabilitiesLog.created_at >= cutoff_utc)
        )
    ).scalar() or 0
    rejected_count = max(reviewed_count - approved_count, 0)

    # --- 4) 组路由命中 (usage_logs 按 group_id) ---
    group_rows = (
        await session.execute(
            select(UsageLog.group_id, func.count(UsageLog.id))
            .where(UsageLog.created_at >= cutoff_local)
            .group_by(UsageLog.group_id)
        )
    ).all()
    total_requests = sum(row[1] for row in group_rows)
    group_routing_items = []
    grouped_requests = 0
    for group_id, cnt in sorted(group_rows, key=lambda r: -r[1]):
        label = group_id or "(精确路由)"
        if group_id and group_id.startswith("tier:"):
            grouped_requests += cnt
        pct = (cnt / total_requests * 100) if total_requests else 0.0
        group_routing_items.append({"group_id": label, "count": cnt, "pct": round(pct, 1)})

    # rag_demo embedding/rerank 流量单列 (2026-09-09 起 rag_demo 直连 aimc):
    # usage_logs.model 存上游 raw 名, 非 LLM 计量面, 单列避免混入 LLM 调量曲线
    rag_bge_names = ["BAAI/bge-m3", "BAAI/bge-reranker-v2-m3"]
    rag_bge_count = (
        await session.execute(
            select(func.count(UsageLog.id))
            .where(UsageLog.created_at >= cutoff_local)
            .where(UsageLog.model.in_(rag_bge_names))
        )
    ).scalar() or 0

    # --- 5) infer_tier 盲区 (metadata 行 new_value JSON inferred_tier=null) ---
    meta_rows = (
        await session.execute(
            select(ModelCapabilitiesLog.new_value)
            .where(ModelCapabilitiesLog.source == "ai_infer")
            .where(ModelCapabilitiesLog.field_changed == "metadata")
        )
    ).scalars().all()
    meta_total = 0
    meta_tier_null = 0
    for nv in meta_rows:
        try:
            obj = json.loads(nv) if nv else {}
        except (ValueError, TypeError):
            continue
        if not isinstance(obj, dict):
            continue
        meta_total += 1
        if obj.get("inferred_tier") is None:
            meta_tier_null += 1

    return {
        "days": days,
        # V6.4 S3 (2026-09-11): AUTO_JOIN_NON_T0=True 已生效 + confidence 门槛, 只读快照
        "auto_join_enabled": _auto_join_non_t0(),
        "auto_join_min_confidence": _auto_join_min_confidence(),
        "pending_review": {
            "total": pending_total,
            "weekly": pending_weekly,
        },
        "tier_distribution": tier_distribution,
        "review_burden": {
            "reviewed_count": reviewed_count,
            "approved_count": approved_count,
            "rejected_count": rejected_count,
            "note": "批准/拒绝自 2026-09-09 起可区分 (approved: 1=批准/-1=拒绝); "
                    "此前处理的行均存为 1, 计入批准数",
        },
        "group_routing": {
            "total_requests": total_requests,
            "grouped_requests": grouped_requests,
            "grouped_pct": round(grouped_requests / total_requests * 100, 1) if total_requests else 0.0,
            "items": group_routing_items,
        },
        "rag_embedding_rerank": {
            "count": rag_bge_count,
            "pct": round(rag_bge_count / total_requests * 100, 1) if total_requests else 0.0,
            "note": "rag_demo 的 /v1/embeddings、/v1/rerank 流量 (2026-09-09 起直连 aimc), "
                    "按上游 raw 名 BAAI/bge-m3、BAAI/bge-reranker-v2-m3 统计; "
                    "非 LLM 计量面, 对比 LLM 调量曲线时请扣除",
        },
        "tier_blind_spot": {
            "metadata_total": meta_total,
            "tier_null": meta_tier_null,
            "pct": round(meta_tier_null / meta_total * 100, 1) if meta_total else 0.0,
        },
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
    # V4.8.5 fix: 补局部 import, 否则 NameError 被 except 吞掉, 缓存实际从不刷新
    try:
        from gateway.router import route_engine
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

    body: { "api_key": "<new-api-key>" }

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


# ============================================================
# V4.8: Dashboard 多用户 — 按人聚合用量 + Key 管理
# ============================================================


@router.get("/api/stats/per-user")
async def get_per_user_usage(
    days: int = Query(7, ge=1, le=90),
    session: AsyncSession = Depends(get_session),
):
    """V4.8: 按人聚合用量 (caller_id GROUP BY)

    LEFT JOIN users, NULL caller_id 归为 "(未鉴权/legacy)"。

    V4.8.1: 增加 per-key label 下钻 — 每个用户行内嵌 keys 数组,
    包含该用户各 api_key 的 label + 用量明细。支持前端展开行查看。
    """
    cutoff = datetime.now() - timedelta(days=days)

    # 1. 用户级聚合 (现有逻辑)
    user_stmt = (
        select(
            UsageLog.caller_id,
            User.username,
            User.display_name,
            func.coalesce(func.count(UsageLog.id), 0).label("request_count"),
            func.coalesce(func.sum(UsageLog.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(UsageLog.output_tokens), 0).label("output_tokens"),
            func.coalesce(func.sum(UsageLog.reasoning_tokens), 0).label("reasoning_tokens"),
            func.coalesce(func.sum(UsageLog.cost_estimate), 0).label("total_cost"),
        )
        .outerjoin(User, UsageLog.caller_id == User.id)
        .where(UsageLog.created_at >= cutoff)
        .group_by(UsageLog.caller_id, User.username, User.display_name)
        .order_by(
            (func.sum(UsageLog.input_tokens) + func.sum(UsageLog.output_tokens)).desc()
        )
    )
    user_rows = (await session.execute(user_stmt)).all()

    # 2. key 级聚合 (按 caller_id + key_id GROUP BY, LEFT JOIN api_keys 取 label)
    key_stmt = (
        select(
            UsageLog.caller_id,
            UsageLog.key_id,
            ApiKey.label,
            func.coalesce(func.count(UsageLog.id), 0).label("request_count"),
            func.coalesce(func.sum(UsageLog.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(UsageLog.output_tokens), 0).label("output_tokens"),
            func.coalesce(func.sum(UsageLog.reasoning_tokens), 0).label("reasoning_tokens"),
            func.coalesce(func.sum(UsageLog.cost_estimate), 0).label("total_cost"),
        )
        .outerjoin(ApiKey, UsageLog.key_id == ApiKey.id)
        .where(UsageLog.created_at >= cutoff)
        .where(UsageLog.key_id.isnot(None))
        .group_by(UsageLog.caller_id, UsageLog.key_id, ApiKey.label)
    )
    key_rows = (await session.execute(key_stmt)).all()

    # 3. 组装: key_map[caller_id] = [key_detail, ...]
    key_map: dict[int, list] = {}
    for row in key_rows:
        cid = row.caller_id
        if cid not in key_map:
            key_map[cid] = []
        key_map[cid].append({
            "key_id": row.key_id,
            "label": row.label or "(无标签)",
            "request_count": row.request_count,
            "input_tokens": int(row.input_tokens or 0),
            "output_tokens": int(row.output_tokens or 0),
            "reasoning_tokens": int(row.reasoning_tokens or 0),  # V6.x 观察期指标
            "total_tokens": int((row.input_tokens or 0) + (row.output_tokens or 0)),
            "total_cost": round(float(row.total_cost or 0), 4),
        })

    items = []
    for row in user_rows:
        items.append({
            "caller_id": row.caller_id,
            "username": row.username or "(未鉴权/legacy)",
            "display_name": row.display_name,
            "request_count": row.request_count,
            "input_tokens": int(row.input_tokens or 0),
            "output_tokens": int(row.output_tokens or 0),
            "reasoning_tokens": int(row.reasoning_tokens or 0),  # V6.x 观察期指标
            "total_tokens": int((row.input_tokens or 0) + (row.output_tokens or 0)),
            "total_cost": round(float(row.total_cost or 0), 4),
            "keys": key_map.get(row.caller_id, []),
        })
    return {"count": len(items), "days": days, "items": items}


def _api_key_to_dict(k: ApiKey) -> dict:
    """ApiKey 转脱敏 dict (永不返回 key_hash)"""
    return {
        "id": k.id,
        "key_prefix": k.key_prefix,
        "is_admin": bool(k.is_admin),
        "status": k.status,
        "label": k.label,
        "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
        "created_at": k.created_at.isoformat() if k.created_at else None,
    }


@router.get("/api/keys")
async def list_user_keys(session: AsyncSession = Depends(get_session)):
    """V4.8: 列出所有 user + 其 api_key (脱敏, 不含 key_hash)"""
    users = (await session.execute(
        select(User).order_by(User.id)
    )).scalars().all()

    result = []
    for u in users:
        keys = (await session.execute(
            select(ApiKey).where(ApiKey.user_id == u.id).order_by(ApiKey.id)
        )).scalars().all()
        result.append({
            "id": u.id,
            "username": u.username,
            "display_name": u.display_name,
            "role": u.role,
            "status": u.status,
            "daily_token_limit": u.daily_token_limit,
            "monthly_token_limit": u.monthly_token_limit,
            "notes": u.notes,
            "keys": [_api_key_to_dict(k) for k in keys],
        })
    return {"count": len(result), "users": result}


@router.post("/api/keys/{key_id}/revoke")
async def revoke_api_key(key_id: int, session: AsyncSession = Depends(get_session)):
    """V4.8: 吊销 api_key (status active → revoked, 软删)

    幂等: 已 revoked 的 key 再调也返回 ok。
    清 middleware 的 _apikey_cache 使吊销立即生效 (否则最迟 30s)。
    """
    api_key = await session.get(ApiKey, key_id)
    if not api_key:
        raise HTTPException(status_code=404, detail=f"Key 不存在: {key_id}")

    if api_key.status != "revoked":
        api_key.status = "revoked"
        await session.commit()
        logger.info("V4.8 dashboard revoke: key_id=%s user_id=%s prefix=%s",
                     key_id, api_key.user_id, api_key.key_prefix)

        # 清 middleware 鉴权缓存, 使吊销立即生效
        try:
            import gateway.middleware as mw
            mw._apikey_cache.clear()
        except Exception as e:
            logger.warning("清 middleware _apikey_cache 失败 (非致命, 30s 后自动过期): %s", e)

    return {"status": "revoked", "key": _api_key_to_dict(api_key)}


@router.post("/api/keys", status_code=201)
async def create_user_key(payload: dict, session: AsyncSession = Depends(get_session)):
    """V6.1 (2026-09-03): 创建/复用 user + 发一把新 api_key (等价 add_user_key.py)。

    V6.1 架构友好化: 此前"发 key"只有 CLI (scripts/add_user_key.py), agent 无法
    通过 REST 完成 — 现补齐。语义与 CLI 对齐:
    - username 已存在 → 复用该 user, 只加新 key
    - username 不存在 → 新建 user (role 默认: admin=true → admin, 否则 user)
    - key 明文只在响应里出现一次, 库里只存 SHA256 hash

    body: {
        "username": "zhangsan",              # 必填
        "display_name": "张三",               # 可选
        "label": "笔记本",                    # 可选, key 标签
        "admin": false,                      # 可选, 发 admin key (可访问 dashboard 全数据)
        "daily_limit": 100000,               # 可选, 每日 token 限额 (NULL=不限)
        "monthly_limit": 1000000,            # 可选, 每月 token 限额 (NULL=不限)
        "notes": "产品组"                    # 可选, 用户备注
    }
    返回 201: { user_id, key_id, key_prefix, label, key_plain }
    """
    import hashlib
    import secrets

    username = (payload.get("username") or "").strip()
    if not username:
        raise HTTPException(status_code=400, detail="username 必填")

    is_admin = bool(payload.get("admin", False))
    role = payload.get("role") or ("admin" if is_admin else "user")

    # 查或建 user (与 add_user_key.py 语义一致)
    user = (await session.execute(
        select(User).where(User.username == username)
    )).scalars().first()
    if user is None:
        user = User(
            username=username,
            display_name=payload.get("display_name") or username,
            role=role,
            status="active",
            daily_token_limit=payload.get("daily_limit"),
            monthly_token_limit=payload.get("monthly_limit"),
            notes=payload.get("notes"),
        )
        session.add(user)
        await session.flush()
    elif user.status != "active":
        raise HTTPException(status_code=400, detail=f"user {username} 状态 {user.status}, 不能发 key")

    # 生成 key: 明文只返回一次, 库里只存 hash
    key_plain = "aimc-" + secrets.token_urlsafe(32)
    api_key = ApiKey(
        user_id=user.id,
        key_hash=hashlib.sha256(key_plain.encode()).hexdigest(),
        key_prefix=key_plain[:8],
        is_admin=1 if is_admin else 0,
        status="active",
        label=payload.get("label"),
    )
    session.add(api_key)
    await session.commit()
    logger.info("V6.1 dashboard create key: user_id=%s username=%s key_id=%s admin=%s",
                user.id, username, api_key.id, is_admin)

    return {
        "user_id": user.id,
        "key_id": api_key.id,
        "key_prefix": api_key.key_prefix,
        "label": api_key.label,
        "key_plain": key_plain,
    }


# ============================================================
# V4.8.4: 低置信度优惠审查队列
# ============================================================

@router.get("/api/promotion-reviews")
async def list_promotion_reviews(
    limit: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
):
    """V4.8.4: 列出待审低置信度优惠

    数据源: promotion_review_queue WHERE approved=0
    按创建时间倒序。
    """
    rows = await crud.list_pending_promotion_reviews(session, limit=limit)
    items = []
    for r in rows:
        items.append({
            "id": r.id,
            "provider": r.provider,
            "title": r.title,
            "summary": r.summary,
            "url": r.url,
            "confidence": r.confidence,
            "confidence_level": (
                "high" if (r.confidence or 0) >= 0.8
                else "medium" if (r.confidence or 0) >= 0.6
                else "low"
            ),
            "created_at": r.created_at.isoformat() if r.created_at else None,
        })
    return {
        "count": len(items),
        "items": items,
    }


@router.post("/api/promotion-reviews/{review_id}/approve")
async def approve_promotion_review(
    review_id: int,
    session: AsyncSession = Depends(get_session),
):
    """V4.8.4: 批准低置信度优惠 → 从审查队列取出, 入 promotions 表

    流程:
    1. 从 raw_data 反序列化 ParseResult
    2. 调 _save_promotion 入 Promotion 表 (含去重 + sanity check)
    3. 审查记录标记 approved=1, action="approved"
    """
    review = await crud.get_promotion_review(session, review_id)
    if not review:
        raise HTTPException(status_code=404, detail=f"Review {review_id} not found")
    if review.approved == 1:
        raise HTTPException(status_code=400, detail="已处理")

    # 从 raw_data 反序列化 ParseResult
    from schemas.intel import ParseResult
    from intelligence.healer import _save_promotion
    from types import SimpleNamespace

    raw = json.loads(review.raw_data) if review.raw_data else {}
    # 去掉 _save_promotion 内部会添加的 _source_type / _intelligence_kind
    raw.pop("_source_type", None)
    raw.pop("_intelligence_kind", None)
    try:
        result = ParseResult(**raw)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"raw_data 反序列化失败: {e}",
        )

    # 构造轻量 source 对象供 _save_promotion 使用
    fake_source = SimpleNamespace(
        provider=review.provider,
        source_type=raw.get("source_type", "web_page"),
        url=review.url or "",
    )

    # 入 Promotion 表 (内部含 7 天去重 + sanity check + model_sync)
    await _save_promotion(result, fake_source)

    # 标记审查记录
    review.approved = 1
    review.action = "approved"
    review.reviewed_at = datetime.now()
    await session.commit()

    logger.info(f"优惠审查批准入库: review_id={review_id} provider={review.provider}")
    return {
        "status": "approved",
        "review_id": review_id,
        "provider": review.provider,
    }


@router.post("/api/promotion-reviews/{review_id}/reject")
async def reject_promotion_review(
    review_id: int,
    session: AsyncSession = Depends(get_session),
):
    """V4.8.4: 拒绝低置信度优惠 → 不入 promotions 表, 保留审查记录

    审查记录标记 approved=1, action="rejected", 留底供后续审计。
    """
    review = await crud.get_promotion_review(session, review_id)
    if not review:
        raise HTTPException(status_code=404, detail=f"Review {review_id} not found")
    if review.approved == 1:
        raise HTTPException(status_code=400, detail="已处理")

    review.approved = 1
    review.action = "rejected"
    review.reviewed_at = datetime.now()
    await session.commit()

    logger.info(f"优惠审查拒绝: review_id={review_id} provider={review.provider}")
    return {
        "status": "rejected",
        "review_id": review_id,
    }


# ============================================================
# V4.9.3: 请求/响应转录查询 API (缓存诊断)
# ============================================================

@router.get("/api/transcripts")
async def list_transcripts(
    days: int = Query(3, ge=1, le=30, description="最近 N 天"),
    limit: int = Query(100, ge=1, le=500, description="最多返回条数"),
    channel_id: Optional[int] = Query(None, description="按 channel 过滤"),
    model: Optional[str] = Query(None, description="按 model 模糊过滤"),
    session: AsyncSession = Depends(get_session),
):
    """V4.9.3: 列出最近的请求转录日志 (不含 body, 概览用)

    按 created_at DESC 排序, 返回 id / channel_id / model / status_code /
    request_len / response_len / created_at。要查完整 body 用 /api/transcripts/{id}。
    """
    from db.models import RequestTranscript

    cutoff = datetime.now() - timedelta(days=days)
    stmt = (
        select(RequestTranscript)
        .where(RequestTranscript.created_at >= cutoff)
        .order_by(RequestTranscript.created_at.desc())
        .limit(limit)
    )
    if channel_id is not None:
        stmt = stmt.where(RequestTranscript.channel_id == channel_id)
    if model:
        stmt = stmt.where(RequestTranscript.model.like(f"%{model}%"))

    result = await session.execute(stmt)
    rows = result.scalars().all()
    return {
        "total": len(rows),
        "transcripts": [r.to_dict(include_body=False) for r in rows],
    }


@router.get("/api/transcripts/{transcript_id}")
async def get_transcript(
    transcript_id: int,
    session: AsyncSession = Depends(get_session),
):
    """V4.9.3: 单条转录详情 (含完整 request/response body)

    用于 A/B 对比时人工检查 cache 开/关下的上游请求差异和响应质量。
    """
    from db.models import RequestTranscript

    result = await session.execute(
        select(RequestTranscript).where(RequestTranscript.id == transcript_id)
    )
    row = result.scalars().first()
    if not row:
        raise HTTPException(status_code=404, detail=f"Transcript {transcript_id} not found")
    return row.to_dict(include_body=True)


# ============================================================
# V6.3.1 (2026-09-06): 瞬时冷却事件流水查询 (抓"现场")
# ============================================================

@router.get("/api/cooldown-events")
async def list_cooldown_events(
    hours: int = Query(24, ge=1, le=168, description="最近 N 小时"),
    limit: int = Query(100, ge=1, le=500, description="最多返回条数"),
    account_id: Optional[int] = Query(None, description="按账号过滤"),
    session: AsyncSession = Depends(get_session),
):
    """V6.3.1: 列出最近观察到的瞬时冷却事件 (运维页历史卡用)

    数据来源: /api/pending-summary 观察到非零瞬时冷却时落库的 cooldown_events
    (badge 是否 exclude_transient 都会落库)。同一 episode 只有一条 —
    cooldown_until 是去重键。
    """
    cutoff = datetime.now() - timedelta(hours=hours)
    result = await session.execute(
        select(CooldownEvent)
        .where(CooldownEvent.created_at >= cutoff,
               CooldownEvent.account_id == account_id)
        .order_by(CooldownEvent.created_at.desc())
        .limit(limit)
    ) if account_id is not None else await session.execute(
        select(CooldownEvent)
        .where(CooldownEvent.created_at >= cutoff)
        .order_by(CooldownEvent.created_at.desc())
        .limit(limit)
    )
    rows = result.scalars().all()
    return {
        "total": len(rows),
        "events": [{
            "id": r.id,
            "account_id": r.account_id,
            "channel_name": r.channel_name,
            "account_name": r.account_name,
            "error_type": r.error_type,
            "error_code": r.error_code,
            "error_msg": r.error_msg,
            "cooldown_until": r.cooldown_until,
            "remaining_seconds": r.remaining_seconds,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        } for r in rows],
    }
