"""极简 Dashboard 路由

提供：
- /dashboard -> 单页面 HTML
- /api/dashboard/* -> Dashboard 数据 API
- /api/dashboard/sources/run-now -> 手动触发情报采集 (Phase 2A-4)
- /api/dashboard/accounts/* -> V4.6 账号管理 (列表/加/启停/轮换/过期扫描)
"""

import html
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
    """Dashboard 主页面

    V6.10.1 (2026-09-20): 静态资源 7d 强缓存 (app.py _static_cache_headers,
    max-age=604800) 会钉住旧 app.js — index.html 每次都是新拉的 (无 Cache-Control),
    但 /static/app.js / style.css 被浏览器缓存 7 天 → 发版后用户看不到新功能
    (实证: 性能统计卡有 HTML 骨架无数据、range 选择器不跟随)。
    app.py 注释假设"文件名带内容哈希", 但前端并无构建哈希 — 这里在 serving 层
    按资源文件 mtime 注入 ?v= 版本 query 击穿缓存, 每次发版资源变更自动失效。
    """
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        content = index_file.read_text(encoding="utf-8")
        _v = 0
        for _f in ("app.js", "style.css"):
            _p = STATIC_DIR / _f
            if _p.exists():
                _v = max(_v, int(_p.stat().st_mtime))
        if _v:
            content = content.replace(
                'src="/static/app.js"', f'src="/static/app.js?v={_v}"')
            content = content.replace(
                'href="/static/style.css"', f'href="/static/style.css?v={_v}"')
        return HTMLResponse(content=content)
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


# P2 (2026-09-18): Login 暴力破解防护 — 同 IP 5 次错误 / 5 分钟锁定
# 用内存 dict 存 {ip: [timestamp, ...]}，重启清零可接受 (token 够强, 这层是纵深防御)
_LOGIN_FAILS: dict[str, list[float]] = {}
_LOGIN_FAIL_WINDOW = 300          # 5 分钟窗口
_LOGIN_FAIL_MAX = 5               # 最多允许 5 次错误
_LOGIN_FAIL_LOCKOUT_SEC = 300     # 超出后锁定 5 分钟


def _get_client_ip(request: Request) -> str:
    """取客户端真实 IP。Cloudflare Tunnel 注入 CF-Connecting-IP 头，
    X-Forwarded-For 链里的第一个 public IP 作为 fallback。"""
    cf_ip = request.headers.get("CF-Connecting-IP", "").strip()
    if cf_ip:
        return cf_ip
    xff = request.headers.get("X-Forwarded-For", "").strip()
    if xff:
        return xff.split(",")[0].strip()
    host = getattr(request.client, "host", None) if request.client else None
    return host or "unknown"


@router.post("/api/auth/login")
async def dashboard_auth_login(request: Request):
    """P1-7: dashboard 登录端点 (备选 — 浏览器可用 fetch 提交而非 URL query)

    Body: {"token": "<token>"}
    Returns:
    - 200 + Set-Cookie: aimc_dashboard_token=<token>; Secure; HttpOnly; SameSite=Lax (浏览器场景)
    - 401: 错误 token
    - 429: 频繁错误尝试 (5 次/5 分钟/IP, P2 防护)
    """
    from starlette.responses import JSONResponse
    from config import settings
    import hmac, time as _time

    ip = _get_client_ip(request)
    now = _time.time()

    # 检查是否被锁定
    fails = _LOGIN_FAILS.get(ip, [])
    fails = [ts for ts in fails if now - ts < _LOGIN_FAIL_WINDOW]
    _LOGIN_FAILS[ip] = fails
    if len(fails) >= _LOGIN_FAIL_MAX:
        return JSONResponse(
            status_code=429,
            content={"error": {
                "message": f"Too many login attempts. Retry after {_LOGIN_FAIL_LOCKOUT_SEC}s.",
                "type": "rate_limit_error",
            }},
        )

    body = await request.json()
    provided = body.get("token", "").strip()
    if not hmac.compare_digest(provided, settings.dashboard_token):
        fails.append(now)
        _LOGIN_FAILS[ip] = fails
        return JSONResponse(
            status_code=401,
            content={"error": {"message": "Invalid token", "type": "authentication_error"}},
        )
    # 登录成功 — 清除该 IP 的失败记录
    _LOGIN_FAILS.pop(ip, None)
    # 设 cookie (浏览器场景)
    response = JSONResponse(content={"ok": True})
    response.set_cookie(
        key="aimc_dashboard_token",
        value=provided,
        httponly=True,
        samesite="lax",
        secure=True,               # P2: 仅 HTTPS 下发送 (Cloudflare Tunnel 提供 TLS)
        max_age=86400 * 7,         # 7 天
    )
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
    """用量统计摘要 (滚动窗口按时间桶聚合, 缺桶补零)

    口径统一 (2026-09-13): 与全站一致用滚动窗口 cutoff=本地 now-days, 取代旧的
    "最近 N 个有数据的自然日" LIMIT 语义 (无 cutoff 扫全表 + 零流量日凭空消失)。
    days=1 按小时分桶 (近 24 小时分时, 消灭跨日半截桶), 其余按日分桶;
    桶轴在 Python 侧生成, 无数据时段补零行 — 图表不断轴。时间升序 (画图用)。
    """
    cutoff = datetime.now() - timedelta(days=days)
    hourly = days == 1
    bucket = (
        func.strftime("%Y-%m-%d %H:00", UsageLog.created_at)
        if hourly else func.date(UsageLog.created_at)
    )
    rows = {
        r.date: r
        for r in (
            await session.execute(
                select(
                    bucket.label("date"),
                    func.count(UsageLog.id).label("request_count"),
                    func.sum(UsageLog.input_tokens).label("total_input_tokens"),
                    func.sum(UsageLog.output_tokens).label("total_output_tokens"),
                    func.sum(UsageLog.reasoning_tokens).label("total_reasoning_tokens"),
                    func.avg(UsageLog.latency_ms).label("avg_latency_ms"),
                )
                .where(UsageLog.created_at >= cutoff)
                .group_by(bucket)
            )
        ).all()
    }

    # 完整桶轴: 滚动窗口覆盖到的时段全部产出, 含两端截段桶
    now = datetime.now()
    buckets = []
    if hourly:
        slot = cutoff.replace(minute=0, second=0, microsecond=0)
        while slot <= now:
            buckets.append(slot.strftime("%Y-%m-%d %H:00"))
            slot += timedelta(hours=1)
    else:
        day, today = cutoff.date(), now.date()
        while day <= today:
            buckets.append(day.isoformat())
            day += timedelta(days=1)

    return [
        {
            "date": b,
            "request_count": rows[b].request_count if b in rows else 0,
            "total_input_tokens": (rows[b].total_input_tokens or 0) if b in rows else 0,
            "total_output_tokens": (rows[b].total_output_tokens or 0) if b in rows else 0,
            # V6.x: 思考 token (关思考评估观察期指标, NULL 行不参与 SUM)
            "total_reasoning_tokens": (rows[b].total_reasoning_tokens or 0) if b in rows else 0,
            "avg_latency_ms": round(rows[b].avg_latency_ms or 0, 1) if b in rows else 0.0,
        }
        for b in buckets
    ]


def _nearest_rank_percentile(values: list, ratio: float) -> float:
    """Python 侧 percentile (SQLite 无 percentile 聚合) — nearest-rank。

    同 scripts/aggregate_weekly_feedback.py 口径 (p50/p95 沉淀 feedback_summary).
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = int(len(ordered) * ratio)
    return float(ordered[min(idx, len(ordered) - 1)])


def _resolve_key_label_map(key_rows) -> dict:
    """key 显示名消歧 (复用于 by-user / perf / user-trend)。

    key_rows: 每行含 .key_id (None=legacy) / .label / .username (key 所属用户)
    同名 label 跨用户时返回 f"{label} ({username})", key_id=None → "(legacy/env)"。
    """
    label_owners: dict[str, set] = {}
    for r in key_rows:
        if r.label and r.username:
            label_owners.setdefault(r.label, set()).add(r.username)
    out: dict = {None: "(legacy/env)"}
    for r in key_rows:
        if r.key_id is None or r.key_id in out:
            continue
        lbl = r.label or "(无标签)"
        if r.label and r.username and len(label_owners.get(r.label, set())) > 1:
            lbl = f"{r.label} ({r.username})"
        out[r.key_id] = lbl
    return out


@router.get("/api/usage/perf")
async def get_usage_perf(
    days: int = Query(7, ge=1, le=90),
    dim: str = Query("model"),
    session: AsyncSession = Depends(get_session),
):
    """V6.10 (2026-09-20): 性能统计 — 成功请求 (2xx/3xx) 的延迟分位 / 有效吞吐 / TTFT。

    V6.12 (2026-09-23): dim 扩 user | key (原 model | channel | account)。
    口径:
    - dim=user: caller 维 (NULL → "(未鉴权/legacy)")
    - dim=key: key 维 (key_id NULL → "(legacy/env)"; 同名 label 跨用户追加 username 消歧)
    - p50/p95 latency: nearest-rank (Python 侧, 配合 avg 看分布)
    - eff_tok_per_s: Σ output_tokens / Σ latency(秒) — 「有效吞吐」, 含 prefill
      时长, 大上下文请求会被 prefill 稀释, 横向对比需结合 avg_input_tokens
    - ttft_ms: avg(first_token_ms) 非空行均值, ttft_n 为采集覆盖数
      (first_token_ms 为 V6.10 新采集列, 部署初期覆盖率为 0, 列显示「—」)
    - cache_pct: 缓存读取 tokens / 总输入 (0 输入防护)
    """
    cutoff = datetime.now() - timedelta(days=days)

    if dim == "user":
        # caller 维: display_name 优先 (与成本页用户列一致)
        key_expr = func.coalesce(
            func.nullif(User.display_name, ""), User.username, "(未鉴权/legacy)"
        )
        rows = (
            await session.execute(
                select(
                    key_expr.label("key"),
                    UsageLog.latency_ms,
                    UsageLog.input_tokens,
                    UsageLog.output_tokens,
                    UsageLog.cache_read_input_tokens,
                    UsageLog.first_token_ms,
                )
                .join(User, UsageLog.caller_id == User.id, isouter=True)
                .where(
                    UsageLog.created_at >= cutoff,
                    UsageLog.status_code >= 200,
                    UsageLog.status_code < 400,
                )
            )
        ).all()
    elif dim == "key":
        # key 维: key_id 分组 + Python 侧 label 消歧 (含 legacy NULL key)
        key_rows = (
            await session.execute(
                select(
                    UsageLog.key_id,
                    ApiKey.label,
                    User.username,
                    UsageLog.latency_ms,
                    UsageLog.input_tokens,
                    UsageLog.output_tokens,
                    UsageLog.cache_read_input_tokens,
                    UsageLog.first_token_ms,
                )
                .join(ApiKey, UsageLog.key_id == ApiKey.id, isouter=True)
                .join(User, ApiKey.user_id == User.id, isouter=True)
                .where(
                    UsageLog.created_at >= cutoff,
                    UsageLog.status_code >= 200,
                    UsageLog.status_code < 400,
                )
            )
        ).all()
        label_map = _resolve_key_label_map(key_rows)
        groups: dict = {}
        for r in key_rows:
            key = label_map.get(r.key_id, "(legacy/env)")
            g = groups.setdefault(
                key,
                {"n": 0, "lats": [], "tot_in": 0, "tot_out": 0,
                 "cache_read": 0, "ttft": []},
            )
            g["n"] += 1
            if r.latency_ms is not None:
                g["lats"].append(r.latency_ms)
            g["tot_in"] += r.input_tokens or 0
            g["tot_out"] += r.output_tokens or 0
            g["cache_read"] += r.cache_read_input_tokens or 0
            if r.first_token_ms is not None:
                g["ttft"].append(r.first_token_ms)
        items = []
        for key, g in groups.items():
            tot_lat_s = sum(g["lats"]) / 1000.0
            items.append({
                "key": key,
                "n": g["n"],
                "avg_latency_ms": round(sum(g["lats"]) / len(g["lats"]), 1) if g["lats"] else None,
                "p50_latency_ms": _nearest_rank_percentile(g["lats"], 0.50),
                "p95_latency_ms": _nearest_rank_percentile(g["lats"], 0.95),
                "avg_input_tokens": round(g["tot_in"] / g["n"], 1),
                "avg_output_tokens": round(g["tot_out"] / g["n"], 1),
                "eff_tok_per_s": round(g["tot_out"] / tot_lat_s, 2) if tot_lat_s > 0 else 0.0,
                "cache_pct": round(100.0 * g["cache_read"] / g["tot_in"], 1) if g["tot_in"] else 0.0,
                "ttft_ms": round(sum(g["ttft"]) / len(g["ttft"]), 1) if g["ttft"] else None,
                "ttft_n": len(g["ttft"]),
            })
        items.sort(key=lambda it: it["n"], reverse=True)
        return {"days": days, "dim": dim, "items": items}
    else:
        dim_col = {
            "model": UsageLog.model,
            "channel": Channel.name,
            "account": Account.account_name,
        }
        col = dim_col.get(dim, UsageLog.model)
        rows = (
            await session.execute(
                select(
                    col.label("key"),
                    UsageLog.latency_ms,
                    UsageLog.input_tokens,
                    UsageLog.output_tokens,
                    UsageLog.cache_read_input_tokens,
                    UsageLog.first_token_ms,
                )
                .join(Channel, UsageLog.channel_id == Channel.id, isouter=True)
                .join(Account, UsageLog.account_id == Account.id, isouter=True)
                .where(
                    UsageLog.created_at >= cutoff,
                    UsageLog.status_code >= 200,
                    UsageLog.status_code < 400,
                )
            )
        ).all()

    groups: dict = {}
    for r in rows:
        key = r.key or "(未知)"
        g = groups.setdefault(
            key,
            {"n": 0, "lats": [], "tot_in": 0, "tot_out": 0,
             "cache_read": 0, "ttft": []},
        )
        g["n"] += 1
        if r.latency_ms is not None:
            g["lats"].append(r.latency_ms)
        g["tot_in"] += r.input_tokens or 0
        g["tot_out"] += r.output_tokens or 0
        g["cache_read"] += r.cache_read_input_tokens or 0
        if r.first_token_ms is not None:
            g["ttft"].append(r.first_token_ms)

    items = []
    for key, g in groups.items():
        tot_lat_s = sum(g["lats"]) / 1000.0
        items.append({
            "key": key,
            "n": g["n"],
            "avg_latency_ms": round(sum(g["lats"]) / len(g["lats"]), 1) if g["lats"] else None,
            "p50_latency_ms": _nearest_rank_percentile(g["lats"], 0.50),
            "p95_latency_ms": _nearest_rank_percentile(g["lats"], 0.95),
            "avg_input_tokens": round(g["tot_in"] / g["n"], 1),
            "avg_output_tokens": round(g["tot_out"] / g["n"], 1),
            "eff_tok_per_s": round(g["tot_out"] / tot_lat_s, 2) if tot_lat_s > 0 else 0.0,
            "cache_pct": round(100.0 * g["cache_read"] / g["tot_in"], 1) if g["tot_in"] else 0.0,
            "ttft_ms": round(sum(g["ttft"]) / len(g["ttft"]), 1) if g["ttft"] else None,
            "ttft_n": len(g["ttft"]),
        })
    items.sort(key=lambda it: it["n"], reverse=True)
    return {"days": days, "dim": dim, "items": items}


@router.get("/api/usage/by-user")
async def get_usage_by_user(
    days: int = Query(7, ge=1, le=90),
    session: AsyncSession = Depends(get_session),
):
    """V6.12 (2026-09-23): 按用户/Key 聚合用量 — 量视角 (用量页「用户与 Key 用量」卡数据源)。

    与成本页 /api/stats/per-user 分开维护:
    - 后者服务成本页按用户 tab (key 下钻过滤 legacy, 带成本列)
    - 此端点服务用量页 (key 级含 legacy/env 行 + totals, 不含成本列)

    key 级: key_id NULL → "(legacy/env)" (legacy/env key 流量单列, 不丢失);
    同名 label 跨用户时追加 (username) 消歧 (同 /api/requests 口径)。
    """
    cutoff = datetime.now() - timedelta(days=days)

    # 1) user 级聚合 (caller_id GROUP BY)
    user_rows = (
        await session.execute(
            select(
                UsageLog.caller_id,
                User.username,
                User.display_name,
                func.coalesce(func.count(UsageLog.id), 0).label("request_count"),
                func.coalesce(func.sum(UsageLog.input_tokens), 0).label("input_tokens"),
                func.coalesce(func.sum(UsageLog.output_tokens), 0).label("output_tokens"),
                func.coalesce(func.sum(UsageLog.reasoning_tokens), 0).label("reasoning_tokens"),
            )
            .outerjoin(User, UsageLog.caller_id == User.id)
            .where(UsageLog.created_at >= cutoff)
            .group_by(UsageLog.caller_id, User.username, User.display_name)
            .order_by(
                (func.sum(UsageLog.input_tokens) + func.sum(UsageLog.output_tokens)).desc()
            )
        )
    ).all()

    # 2) key 级聚合 (caller_id + key_id GROUP BY, 含 legacy: 不加 key_id isnot(None) 过滤)
    key_rows = (
        await session.execute(
            select(
                UsageLog.caller_id,
                UsageLog.key_id,
                ApiKey.label,
                User.username,
                func.coalesce(func.count(UsageLog.id), 0).label("request_count"),
                func.coalesce(func.sum(UsageLog.input_tokens), 0).label("input_tokens"),
                func.coalesce(func.sum(UsageLog.output_tokens), 0).label("output_tokens"),
                func.coalesce(func.sum(UsageLog.reasoning_tokens), 0).label("reasoning_tokens"),
            )
            .outerjoin(ApiKey, UsageLog.key_id == ApiKey.id)
            .outerjoin(User, ApiKey.user_id == User.id)
            .where(UsageLog.created_at >= cutoff)
            .group_by(UsageLog.caller_id, UsageLog.key_id, ApiKey.label, User.username)
        )
    ).all()
    key_label_map = _resolve_key_label_map(key_rows)

    # 3) 组装: key_map[caller_id] = [key_detail, ...]
    key_map: dict = {}
    for r in key_rows:
        cid = r.caller_id
        if cid not in key_map:
            key_map[cid] = []
        key_map[cid].append({
            "key_id": r.key_id,
            "label": key_label_map.get(r.key_id, "(legacy/env)"),
            "username": r.username,
            "request_count": r.request_count,
            "input_tokens": int(r.input_tokens or 0),
            "output_tokens": int(r.output_tokens or 0),
            "reasoning_tokens": int(r.reasoning_tokens or 0),
            "total_tokens": int((r.input_tokens or 0) + (r.output_tokens or 0)),
        })
    for keys in key_map.values():
        keys.sort(key=lambda k: k["total_tokens"], reverse=True)

    items = []
    for row in user_rows:
        items.append({
            "caller_id": row.caller_id,
            "username": row.username or "(未鉴权/legacy)",
            "display_name": row.display_name,
            "request_count": row.request_count,
            "input_tokens": int(row.input_tokens or 0),
            "output_tokens": int(row.output_tokens or 0),
            "reasoning_tokens": int(row.reasoning_tokens or 0),
            "total_tokens": int((row.input_tokens or 0) + (row.output_tokens or 0)),
            "keys": key_map.get(row.caller_id, []),
        })

    totals = {
        "request_count": sum(i["request_count"] for i in items),
        "input_tokens": sum(i["input_tokens"] for i in items),
        "output_tokens": sum(i["output_tokens"] for i in items),
        "reasoning_tokens": sum(i["reasoning_tokens"] for i in items),
        "total_tokens": sum(i["total_tokens"] for i in items),
    }
    return {"days": days, "totals": totals, "users": items}


@router.get("/api/usage/user-trend")
async def get_usage_user_trend(
    days: int = Query(7, ge=1, le=90),
    dim: str = Query("user", pattern="^(user|key)$"),
    session: AsyncSession = Depends(get_session),
):
    """V6.12 (2026-09-23): 按用户/Key 的用量趋势 (用量页 stacked bar 数据源)。

    - dim=user: 按 caller_id GROUP BY, 全保留 (≤ 少数几人)
    - dim=key: 按 key_id GROUP BY, top 8 系列 + 其余归 "其他" (series 过多不可读)
    - days=1 按小时分桶, 其余按日分桶 (同 usage/summary 口径);
      桶轴 Python 侧生成缺桶补零 (图表不断轴)
    - 双 metric 均返回: requests (请求数) + tokens (input+output)
    - series 按窗口内总 token 降序
    """
    cutoff = datetime.now() - timedelta(days=days)
    hourly = days == 1
    bucket = (
        func.strftime("%Y-%m-%d %H:00", UsageLog.created_at)
        if hourly else func.date(UsageLog.created_at)
    )

    if dim == "key":
        group_col = UsageLog.key_id
        rows = (
            await session.execute(
                select(
                    bucket.label("bucket"),
                    UsageLog.key_id,
                    func.count(UsageLog.id).label("request_count"),
                    func.coalesce(func.sum(UsageLog.input_tokens), 0).label("input_tokens"),
                    func.coalesce(func.sum(UsageLog.output_tokens), 0).label("output_tokens"),
                )
                .where(UsageLog.created_at >= cutoff)
                .group_by(bucket, UsageLog.key_id)
            )
        ).all()
        # key 显示名: key 表很小, 全表查一次 (同 /api/requests 口径)
        all_keys = (await session.execute(
            select(ApiKey.id.label("key_id"), ApiKey.label, User.username)
            .join(User, ApiKey.user_id == User.id, isouter=True)
        )).all()
        label_map = _resolve_key_label_map(all_keys)
    else:
        group_col = UsageLog.caller_id
        rows = (
            await session.execute(
                select(
                    bucket.label("bucket"),
                    UsageLog.caller_id,
                    func.count(UsageLog.id).label("request_count"),
                    func.coalesce(func.sum(UsageLog.input_tokens), 0).label("input_tokens"),
                    func.coalesce(func.sum(UsageLog.output_tokens), 0).label("output_tokens"),
                )
                .where(UsageLog.created_at >= cutoff)
                .group_by(bucket, UsageLog.caller_id)
            )
        ).all()
        # user 显示名: display_name 优先, NULL → "(未鉴权/legacy)"
        all_users = (await session.execute(
            select(User.id, User.username, User.display_name).order_by(User.id)
        )).all()
        label_map = {u.id: (u.display_name or u.username) for u in all_users}
        label_map[None] = "(未鉴权/legacy)"

    # 聚合原始数据: raw[(bucket, dim_id)] = {requests, tokens}
    raw: dict[tuple, dict] = {}
    dim_totals: dict = {}
    for r in rows:
        dim_id = r.key_id if dim == "key" else r.caller_id
        tot = int((r.input_tokens or 0) + (r.output_tokens or 0))
        k = (r.bucket, dim_id)
        if k not in raw:
            raw[k] = {"requests": 0, "tokens": 0}
        raw[k]["requests"] += r.request_count
        raw[k]["tokens"] += tot
        dim_totals[dim_id] = dim_totals.get(dim_id, 0) + tot

    # dim=key: top 8 系列 + 其余归 "其他"
    if dim == "key" and len(dim_totals) > 8:
        sorted_dims = sorted(dim_totals.items(), key=lambda x: x[1], reverse=True)
        top_ids = {d for d, _ in sorted_dims[:8]}
        merged: dict[tuple, dict] = {}
        for (b, did), m in raw.items():
            nid = did if did in top_ids else "__other__"
            nk = (b, nid)
            if nk not in merged:
                merged[nk] = {"requests": 0, "tokens": 0}
            merged[nk]["requests"] += m["requests"]
            merged[nk]["tokens"] += m["tokens"]
        raw = merged
        label_map["__other__"] = "其他"

    # 完整桶轴 (含两端截段桶), 时间升序 (画图用)
    now = datetime.now()
    buckets: list[str] = []
    if hourly:
        slot = cutoff.replace(minute=0, second=0, microsecond=0)
        while slot <= now:
            buckets.append(slot.strftime("%Y-%m-%d %H:00"))
            slot += timedelta(hours=1)
    else:
        day, today = cutoff.date(), now.date()
        while day <= today:
            buckets.append(day.isoformat())
            day += timedelta(days=1)
    bucket_idx = {b: i for i, b in enumerate(buckets)}

    # series 组装: 按 dim 总量降序, 桶内缺数据补零
    series_ids = sorted(dim_totals.keys(), key=lambda d: dim_totals[d], reverse=True)
    if dim == "key" and len(dim_totals) > 8:
        series_ids = series_ids[:8] + ["__other__"]
    series = []
    for sid in series_ids:
        req = [0] * len(buckets)
        tok = [0] * len(buckets)
        for (b, did), m in raw.items():
            if did == sid and b in bucket_idx:
                req[bucket_idx[b]] += m["requests"]
                tok[bucket_idx[b]] += m["tokens"]
        series.append({
            "key": label_map.get(sid, "(未知)"),
            "requests": req,
            "tokens": tok,
        })

    return {"days": days, "dim": dim, "buckets": buckets, "series": series}


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


@router.get("/api/usage/cost-by-channel")
async def get_cost_by_channel(
    days: int = Query(7, ge=1, le=90),
    session: AsyncSession = Depends(get_session),
):
    """V6.13 (2026-09-23): 按渠道拆成本 — 补成本页缺失的「渠道」维度。

    与另外三个维度同构: 回答"哪个渠道在花钱、免费渠道省了多少"。
    channel_id NULL (legacy) → "(未知渠道)"。
    """
    cutoff = datetime.now() - timedelta(days=days)
    rows = (
        await session.execute(
            select(
                UsageLog.channel_id,
                Channel.name.label("channel_name"),
                func.count(UsageLog.id).label("request_count"),
                func.coalesce(func.sum(UsageLog.input_tokens), 0).label("input_tokens"),
                func.coalesce(func.sum(UsageLog.output_tokens), 0).label("output_tokens"),
                func.coalesce(func.sum(UsageLog.cost_estimate), 0).label("cost_yuan"),
                func.coalesce(func.sum(UsageLog.list_cost_estimate), 0).label("list_cost_yuan"),
                func.count(UsageLog.list_cost_estimate).label("list_known_requests"),
            )
            .outerjoin(Channel, UsageLog.channel_id == Channel.id)
            .where(UsageLog.created_at >= cutoff)
            .group_by(UsageLog.channel_id, Channel.name)
            .order_by(func.coalesce(func.sum(UsageLog.list_cost_estimate), 0).desc())
        )
    ).all()

    priced = {
        r.channel_id: float(r.cost_yuan_priced or 0)
        for r in (
            await session.execute(
                select(
                    UsageLog.channel_id,
                    func.coalesce(func.sum(UsageLog.cost_estimate), 0).label("cost_yuan_priced"),
                )
                .where(UsageLog.created_at >= cutoff)
                .where(UsageLog.list_cost_estimate.isnot(None))
                .group_by(UsageLog.channel_id)
            )
        ).all()
    }

    items = []
    for row in rows:
        item = {
            "channel_id": row.channel_id,
            "channel_name": row.channel_name or "(未知渠道)",
        }
        item.update(_cost_audit_row_dict(row, priced.get(row.channel_id, 0.0)))
        items.append(item)
    return {"days": days, "totals": _cost_audit_totals(items), "items": items}


@router.get("/api/usage/cost-by-model-channels")
async def get_cost_by_model_channels(
    days: int = Query(7, ge=1, le=90),
    session: AsyncSession = Depends(get_session),
):
    """V6.13 (2026-09-23): 模型 × 渠道 交叉成本 — 供成本页「按模型」tab 展开下钻。

    同一模型可能挂多渠道 (如 glm-5.2 在免费渠道 vs 付费渠道), 单看模型行只是混合均价;
    展开到渠道才能回答"这个模型在哪买最便宜"。结构镜像 /api/stats/per-user 的 keys 下钻。

    响应: items[].channels[] (该模型的渠道明细, 按标价降序); totals 与 cost-by-model 一致。
    """
    cutoff = datetime.now() - timedelta(days=days)
    rows = (
        await session.execute(
            select(
                UsageLog.model,
                UsageLog.channel_id,
                Channel.name.label("channel_name"),
                func.count(UsageLog.id).label("request_count"),
                func.coalesce(func.sum(UsageLog.input_tokens), 0).label("input_tokens"),
                func.coalesce(func.sum(UsageLog.output_tokens), 0).label("output_tokens"),
                func.coalesce(func.sum(UsageLog.cost_estimate), 0).label("cost_yuan"),
                func.coalesce(func.sum(UsageLog.list_cost_estimate), 0).label("list_cost_yuan"),
                func.count(UsageLog.list_cost_estimate).label("list_known_requests"),
            )
            .outerjoin(Channel, UsageLog.channel_id == Channel.id)
            .where(UsageLog.created_at >= cutoff)
            .group_by(UsageLog.model, UsageLog.channel_id, Channel.name)
            .order_by(func.coalesce(func.sum(UsageLog.list_cost_estimate), 0).desc())
        )
    ).all()

    # 标价已知行的实付 (按 model+channel 二趟, 同成本审计口径)
    priced = {
        (r.model, r.channel_id): float(r.cost_yuan_priced or 0)
        for r in (
            await session.execute(
                select(
                    UsageLog.model,
                    UsageLog.channel_id,
                    func.coalesce(func.sum(UsageLog.cost_estimate), 0).label("cost_yuan_priced"),
                )
                .where(UsageLog.created_at >= cutoff)
                .where(UsageLog.list_cost_estimate.isnot(None))
                .group_by(UsageLog.model, UsageLog.channel_id)
            )
        ).all()
    }

    # 先按 model+channel 造行, 再聚成 model 级 items (含 channels[] 下钻)
    by_model: dict = {}
    for row in rows:
        child = {
            "channel_id": row.channel_id,
            "channel_name": row.channel_name or "(未知渠道)",
        }
        child.update(_cost_audit_row_dict(row, priced.get((row.model, row.channel_id), 0.0)))
        key = row.model
        if key not in by_model:
            by_model[key] = {"model": row.model or "(未知模型)", "channels": []}
        by_model[key]["channels"].append(child)

    items = []
    for item in by_model.values():
        children = item["channels"]
        items.append({
            "model": item["model"],
            # 模型级聚合 = 其渠道行求和 (与 cost-by-model 同值)
            "request_count": sum(c["request_count"] for c in children),
            "input_tokens": sum(c["input_tokens"] for c in children),
            "output_tokens": sum(c["output_tokens"] for c in children),
            "cost_yuan": round(sum(c["cost_yuan"] for c in children), 4),
            "list_cost_yuan": round(sum(c["list_cost_yuan"] for c in children), 4),
            "saved_yuan": round(sum(c["saved_yuan"] for c in children), 4),
            "list_known_requests": sum(c["list_known_requests"] for c in children),
            "channels": children,
        })
    items.sort(key=lambda it: it["list_cost_yuan"], reverse=True)
    return {"days": days, "totals": _cost_audit_totals(items), "items": items}


@router.get("/api/usage/cost-compare")
async def get_cost_compare(
    days: int = Query(7, ge=1, le=90),
    session: AsyncSession = Depends(get_session),
):
    """V6.13 (2026-09-23): 成本环比 — 当前窗口 vs 等长上一窗口 (KPI 环比箭头数据源)。

    维度无关 (全窗口聚合, 不做分组), 故成本页 4 个 tab 共用一份, 切 tab 不闪。
    窗口口径: current = [now-N, now], previous = [now-2N, now-N] (滚动, 非自然日)。
    """
    now = datetime.now()
    cur_start = now - timedelta(days=days)
    prev_start = now - timedelta(days=2 * days)

    async def _window_totals(start: datetime, end: datetime) -> dict:
        """窗口内 totals: 一次聚合 + 一次"标价已知行的实付"二趟 (同成本审计口径)"""
        row = (await session.execute(
            select(
                func.count(UsageLog.id).label("request_count"),
                func.coalesce(func.sum(UsageLog.input_tokens), 0).label("input_tokens"),
                func.coalesce(func.sum(UsageLog.output_tokens), 0).label("output_tokens"),
                func.coalesce(func.sum(UsageLog.cost_estimate), 0).label("cost_yuan"),
                func.coalesce(func.sum(UsageLog.list_cost_estimate), 0).label("list_cost_yuan"),
                func.count(UsageLog.list_cost_estimate).label("list_known_requests"),
            )
            .where(UsageLog.created_at >= start)
            .where(UsageLog.created_at < end)
        )).one()
        priced = float((await session.execute(
            select(func.coalesce(func.sum(UsageLog.cost_estimate), 0))
            .where(UsageLog.created_at >= start)
            .where(UsageLog.created_at < end)
            .where(UsageLog.list_cost_estimate.isnot(None))
        )).scalar() or 0)
        req = int(row.request_count)
        known = int(row.list_known_requests or 0)
        list_cost = float(row.list_cost_yuan or 0)
        return {
            "request_count": req,
            "input_tokens": int(row.input_tokens or 0),
            "output_tokens": int(row.output_tokens or 0),
            "cost_yuan": round(float(row.cost_yuan or 0), 4),
            "list_cost_yuan": round(list_cost, 4),
            "saved_yuan": round(list_cost - priced, 4),
            "list_known_requests": known,
            "list_coverage_pct": round(known / req * 100, 1) if req else 0.0,
        }

    return {
        "days": days,
        "current": await _window_totals(cur_start, now),
        "previous": await _window_totals(prev_start, cur_start),
    }


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
                # V6.13 fix: 前端用户 tab 有「思考」列, 此前该端点不返回此字段 → 永远显示 —
                func.coalesce(func.sum(UsageLog.reasoning_tokens), 0).label("reasoning_tokens"),
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
        # V6.13: 思考 token 仅 user tab 展示 (model/group 端点不查该列, 故不进共享行字典)
        item["reasoning_tokens"] = int(row.reasoning_tokens or 0)
        items.append(item)
    return {"days": days, "totals": _cost_audit_totals(items), "items": items}


@router.get("/api/usage/cost-trend")
async def get_cost_trend(
    days: int = Query(7, ge=1, le=90),
    session: AsyncSession = Depends(get_session),
):
    """V6.0 Phase 2: 成本趋势 (标价 vs 实付 vs 节省), 时间升序 (画图用)

    口径统一 (2026-09-13): days=1 按小时分桶 (近 24 小时分时), 其余按日;
    桶轴 Python 侧生成缺桶补零 — 图表不断轴。totals 仍只算窗口内真实数据,
    与 cost-by-* 端点可对账。
    """
    cutoff = datetime.now() - timedelta(days=days)
    hourly = days == 1
    bucket = (
        func.strftime("%Y-%m-%d %H:00", UsageLog.created_at)
        if hourly else func.date(UsageLog.created_at)
    )
    rows = {
        r.date: r
        for r in (
            await session.execute(
                select(
                    bucket.label("date"),
                    func.count(UsageLog.id).label("request_count"),
                    func.coalesce(func.sum(UsageLog.input_tokens), 0).label("input_tokens"),
                    func.coalesce(func.sum(UsageLog.output_tokens), 0).label("output_tokens"),
                    func.coalesce(func.sum(UsageLog.cost_estimate), 0).label("cost_yuan"),
                    func.coalesce(func.sum(UsageLog.list_cost_estimate), 0).label("list_cost_yuan"),
                    func.count(UsageLog.list_cost_estimate).label("list_known_requests"),
                )
                .where(UsageLog.created_at >= cutoff)
                .group_by(bucket)
            )
        ).all()
    }

    priced = {
        r.date: float(r.cost_yuan_priced or 0)
        for r in (
            await session.execute(
                select(
                    bucket.label("date"),
                    func.coalesce(func.sum(UsageLog.cost_estimate), 0).label("cost_yuan_priced"),
                )
                .where(UsageLog.created_at >= cutoff)
                .where(UsageLog.list_cost_estimate.isnot(None))
                .group_by(bucket)
            )
        ).all()
    }

    # 完整桶轴 (含两端截段桶), 补零行保证图上 x 轴连续
    now = datetime.now()
    buckets = []
    if hourly:
        slot = cutoff.replace(minute=0, second=0, microsecond=0)
        while slot <= now:
            buckets.append(slot.strftime("%Y-%m-%d %H:00"))
            slot += timedelta(hours=1)
    else:
        day, today = cutoff.date(), now.date()
        while day <= today:
            buckets.append(day.isoformat())
            day += timedelta(days=1)

    items = []
    for b in buckets:
        if b in rows:
            item = {"date": b}
            item.update(_cost_audit_row_dict(rows[b], priced.get(b, 0.0)))
        else:
            item = {
                "date": b, "request_count": 0, "input_tokens": 0, "output_tokens": 0,
                "cost_yuan": 0.0, "list_cost_yuan": 0.0, "saved_yuan": 0.0,
                "list_known_requests": 0,
            }
        items.append(item)
    return {"days": days, "totals": _cost_audit_totals(items), "items": items}


def _build_cache_price_maps(price_rows) -> tuple[dict, dict]:
    """V6.13: 从 price_snapshots 行构建缓存价查找表 (按渠道×模型 + 模型兜底)。

    返回 (by_ch_model, by_model), 值形如 {"input": float|None, "cached": float|None}。

    取价规则 (与 gateway.proxy._estimate_cost 计费口径对齐):
    - 价格存在 normal/peak/cache_hit 等多种 price_type 行里, 缓存价在
      cached_input_price 列 (生产实测 149 行 normal 中 78 行有值; 无 cache_hit 行)
    - 同一键多行时以 effective_from 最新且该字段非空者为准 (调用方已按
      effective_from 升序传入 → 非空值后写覆盖前写)
    """
    by_ch_model: dict = {}
    by_model: dict = {}

    def _fill(target: dict, key, row) -> None:
        slot = target.setdefault(key, {"input": None, "cached": None})
        if row.input_price is not None:
            slot["input"] = float(row.input_price)
        if row.cached_input_price is not None:
            slot["cached"] = float(row.cached_input_price)

    for r in price_rows:
        _fill(by_ch_model, (r.channel_id, r.model), r)
        _fill(by_model, r.model, r)
    return by_ch_model, by_model


def _cache_saved_yuan(ch_model_prices: dict, model_prices: dict,
                      channel_id, model: str, cached_t: int) -> tuple[float, bool]:
    """V6.13: 缓存节省 (元) — 计费一致口径。

    节省 = 命中 token × (input_price − cached_input_price) / 1e6,
    与 gateway.proxy._estimate_cost 的计费公式互为镜像:
    - 有 cached_input_price → 命中部分按 cached 价计费, 差额即节省
    - 无 cached_input_price → 计费侧按 input_price 兜底 (无优惠) → 节省 0
      (V6.13 前此处用其他渠道/模型的均价 ×9 估算, 会把免费/按次计费渠道的
       命中 token 按付费渠道单价计价, 严重高估)

    返回 (saved_yuan, priced) — priced=False 表示该 (渠道,模型) 无可用缓存价,
    其命中 token 计入"未覆盖", 供前端提示口径可信度。
    """
    if cached_t <= 0:
        return 0.0, True
    slot = ch_model_prices.get((channel_id, model)) or {}
    cached_price = slot.get("cached")
    input_price = slot.get("input")
    if cached_price is None:
        # 该渠道无缓存价 (或无该模型价格行): 保守判为无节省, 不做跨渠道外推
        return 0.0, False
    if input_price is None:
        # 有缓存价但无常规输入价 → 无法算差额, 按 cache 10% 惯例保守估差额
        input_price = cached_price * 10
    diff = max(input_price - cached_price, 0.0)
    return cached_t * diff / 1_000_000, True


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
    # aliy-hy 生产 SQLite 3.26 (<3.30) 不支持 NULLS LAST 语法 → 用 IS NULL 布尔列模拟 (NULL 排最后)
    rows = (await session.execute(
        select(JobHealth).order_by(
            JobHealth.last_success_at.is_(None),
            JobHealth.last_success_at.desc(),
        )
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

    # === 4.5 配额桶隔离中 (V6.17) ===
    # 上游订阅窗口耗尽 (minimax Token Plan 2056) 走模型桶隔离 — 账号级不冷却,
    # 所以不落在上面任何类目, 待处理栏看不见 (A 把 2056 改判配额耗尽后新增
    # 的状态)。桶由 V6.15 恢复探测在窗口刷新后自动放行, 这里给出可见条目 +
    # 手动确认恢复入口。needs_human 账号已由 §1 的余额不足类目呈现, 不重复。
    bucket_aids = [aid for aid, s in snap.items()
                   if s.get("quarantine_models") and not s.get("manual_disabled")
                   and not s.get("needs_human")]
    if bucket_aids and not exclude_transient:
        bucket_examples = []
        for aid in sorted(bucket_aids,
                          key=lambda a: max(snap[a]["quarantine_models"].values()),
                          reverse=True)[:5]:
            s = snap[aid]
            ch_info = _lookup_channel_info(route_engine, aid)
            models = ", ".join(
                f"{m}({rem:.0f}s)" for m, rem in s["quarantine_models"].items()
            )
            msg = s.get("last_error_msg", "")[:100]
            bucket_examples.append({
                "account_id": aid,
                "channel": ch_info["name"] if ch_info else "?",
                "account_name": ch_info.get("account_name", "") if ch_info else "",
                "error_msg": f"配额桶隔离中 {models}" + (f" — {msg}" if msg else ""),
            })
        items.append({
            "category": "quota_bucket",
            "count": len(bucket_aids),
            "severity": "warning",
            "label": "⏳ 配额桶隔离中",
            "anchor": "accounts-meta",
            "examples": bucket_examples,
        })
        total += len(bucket_aids)

    # === 5. 人工禁用 ===
    manual_aids = [aid for aid, s in snap.items()
                   if s.get("manual_disabled")]
    if manual_aids:
        # V6.14: DB 禁用账号不在 _channels_cache (只装 active), 反查不到 →
        # 落 DB 补名, 否则示例行显示 "#11 ?" 丢渠道/账号名 (taotoken_01 实证)
        cache_miss = [aid for aid in manual_aids
                      if _lookup_channel_info(route_engine, aid) is None]
        db_names: dict[int, tuple[str, str]] = {}
        if cache_miss:
            try:
                pairs = await crud.list_accounts(session)
                db_names = {a.id: (ch.name, a.account_name) for a, ch in pairs}
            except Exception as e:
                logger.warning("待处理人工禁用示例 DB 补名失败 (非致命): %s", e)
        manual_examples = []
        for aid in manual_aids[:3]:
            s = snap[aid]
            ch_info = _lookup_channel_info(route_engine, aid)
            if ch_info:
                ch_name, acct_name = ch_info["name"], ch_info.get("account_name", "")
            else:
                ch_name, acct_name = db_names.get(aid, ("?", ""))
            manual_examples.append({
                "account_id": aid,
                "channel": ch_name,
                "account_name": acct_name,
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

    V6.17 (2026-10-21): mark_success 改 force=True 越过迟到成功护栏.
    此前对账号级冷却中的账号 (rate_limited 退避 / auth 24h 冷却) 点
    "确认恢复"会被 early-return 什么都不清, 按钮形同虚设; 用户已核实
    上游恢复是强信号, 应当立即放行 + 立即探活验证, 撞了再冷可接受.
    """
    from gateway.cooldown import cooldown_manager
    from db.engine import async_session_factory
    from db.models import Account
    from datetime import datetime
    from sqlalchemy import update

    cooldown_manager.mark_success(account_id, force=True)
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
    # V6.14: 与 /enable /disable 对齐 — 立即重建路由缓存把账号移出候选
    # (排除主要靠远期 cooldown + DB status 过滤, 这里消灭最长 60s 的缓存窗口;
    #  refresh 里的对账兜底会让 DB 永远赢)
    try:
        from gateway.router import route_engine
        await route_engine.refresh_cache()
    except Exception as e:
        logger.warning("账号 %s 人工禁用后刷新路由缓存失败: %s", account_id, e)
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
    # V6.14: 与 /enable 对齐 — 立即重建路由缓存让账号回到候选 (此前最长 60s
    # 缓存窗口内不可路由; clear_manual_disabled 已同时清远期 cooldown)
    try:
        from gateway.router import route_engine
        await route_engine.refresh_cache()
    except Exception as e:
        logger.warning("账号 %s 解除禁用后刷新路由缓存失败: %s", account_id, e)
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
    # V6.13: 按 (channel, model) 聚合 — 原子粒度, 渠道级/模型级都从它汇总
    detail_rows = (
        await session.execute(
            select(
                UsageLog.channel_id,
                Channel.name.label("channel_name"),
                UsageLog.model,
                func.coalesce(func.sum(UsageLog.input_tokens), 0).label("input_tokens"),
                func.coalesce(func.sum(cache_tokens_expr), 0).label("cached_tokens"),
            )
            .outerjoin(Channel, UsageLog.channel_id == Channel.id)
            .where(UsageLog.created_at >= cutoff)
            .group_by(UsageLog.channel_id, Channel.name, UsageLog.model)
        )
    ).all()

    # 价格表 → (渠道,模型) 缓存价查找表 (effective_from 升序: 新价后写覆盖旧价)
    price_rows = (
        await session.execute(
            select(
                PriceSnapshot.channel_id,
                PriceSnapshot.model,
                PriceSnapshot.input_price,
                PriceSnapshot.cached_input_price,
            ).order_by(PriceSnapshot.effective_from.asc(), PriceSnapshot.id.asc())
        )
    ).all()
    ch_model_prices, model_prices = _build_cache_price_maps(price_rows)

    # 逐 (渠道,模型) 算节省 (计费一致口径), 再向上汇总到渠道/模型
    ch_agg: dict = {}
    model_agg: dict = {}
    for row in detail_rows:
        cached_t = int(row.cached_tokens or 0)
        input_t = int(row.input_tokens or 0)
        saved, priced = _cache_saved_yuan(
            ch_model_prices, model_prices, row.channel_id, row.model, cached_t)

        c = ch_agg.setdefault(row.channel_id, {
            "channel_id": row.channel_id,
            "channel_name": row.channel_name or "(未知渠道)",
            "input_tokens": 0, "cached_tokens": 0,
            "saved_cost_cny": 0.0, "priced_cached_tokens": 0,
        })
        c["input_tokens"] += input_t
        c["cached_tokens"] += cached_t
        c["saved_cost_cny"] += saved
        if priced:
            c["priced_cached_tokens"] += cached_t

        m = model_agg.setdefault(row.model, {
            "model": row.model or "(未知模型)",
            "input_tokens": 0, "cached_tokens": 0,
            "saved_cost_cny": 0.0, "priced_cached_tokens": 0,
        })
        m["input_tokens"] += input_t
        m["cached_tokens"] += cached_t
        m["saved_cost_cny"] += saved
        if priced:
            m["priced_cached_tokens"] += cached_t

    def _pct(part: float, whole: float) -> float:
        """百分比 (分母 0 防护)"""
        return round(part / whole * 100, 1) if whole > 0 else 0.0

    by_channel = []
    for k in sorted(ch_agg.keys(), key=lambda x: -ch_agg[x]["cached_tokens"]):
        agg = ch_agg[k]
        by_channel.append({
            "channel_id": k,
            "channel_name": agg["channel_name"],
            "input_tokens": agg["input_tokens"],
            "cached_tokens": agg["cached_tokens"],
            "hit_rate_pct": _pct(agg["cached_tokens"], agg["input_tokens"]),
            "saved_cost_cny": round(agg["saved_cost_cny"], 4),
            "priced_pct": _pct(agg["priced_cached_tokens"], agg["cached_tokens"]),
        })

    by_model = []
    for k in sorted(model_agg.keys(), key=lambda x: -model_agg[x]["cached_tokens"])[:10]:
        agg = model_agg[k]
        by_model.append({
            "model": k,
            "input_tokens": agg["input_tokens"],
            "cached_tokens": agg["cached_tokens"],
            "hit_rate_pct": _pct(agg["cached_tokens"], agg["input_tokens"]),
            "saved_cost_cny": round(agg["saved_cost_cny"], 4),
            "priced_pct": _pct(agg["priced_cached_tokens"], agg["cached_tokens"]),
        })

    # 总计 (节省 = 各渠道各模型计费差额之和; 只统计有缓存价的命中 token)
    total_input = sum(v["input_tokens"] for v in ch_agg.values())
    total_cached = sum(v["cached_tokens"] for v in ch_agg.values())
    priced_cached = sum(v["priced_cached_tokens"] for v in ch_agg.values())
    total_saved = sum(v["saved_cost_cny"] for v in ch_agg.values())

    return {
        "period_days": days,
        "total_input_tokens": total_input,
        "total_cached_tokens": total_cached,
        "hit_rate_pct": _pct(total_cached, total_input),
        "saved_cost_cny": round(total_saved, 4),
        "priced_pct": _pct(priced_cached, total_cached),
        "priced_cached_tokens": priced_cached,
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
    s.reviewed_at = datetime.now()
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
    s.reviewed_at = datetime.now()
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
            s.applied_at = datetime.now()
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
            s.applied_at = datetime.now()
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
    # usage_logs.created_at 存本地时间 (V6.4.1 全库统一), cutoff 须用本地 now 匹配
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
    # V6.5 S8 (production-review): 总量改不带 limit 的 count — 旧实现先
    # limit(20) 再对截断行求和, 精确路由 distinct model > 20 时 total/
    # exact/group_routed_pct 分母全部偏小 → 路由成功率被高估 (30d 实证
    # distinct=27>20 已触发)。limit(20) 仅用于 top_models 展示。
    unmatched_total = await session.scalar(
        select(func.count(UsageLog.id))
        .where(UsageLog.created_at >= cutoff)
        .where(UsageLog.group_id.is_(None))
    ) or 0
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
    # V6.5 S8: unmatched_total 已改为上方不带 limit 的 count (sum 语义废弃)

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
    caller_id: int = Query(None, ge=1),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
):
    """请求日志明细 (usage_logs), 状态/model/渠道/key 过滤 + 分页, 时间倒序。

    V6.3 F2: model 支持模糊匹配 (LIKE %model%), 如 model=glm 收窄到 glm 系。
    V6.12: caller_id 按用户过滤 (用量页用户维度下钻联动)。
    """
    # usage_logs.created_at 存本地时间 (V6.4.1 全库统一), cutoff 须用本地 now 匹配
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
    if caller_id:
        conds.append(UsageLog.caller_id == caller_id)

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
        "caller_id": caller_id,
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
                "first_token_ms": r.first_token_ms,  # V6.10: TTFT, None=未采集/非流式/错误流
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
    # V6.4.1 时区统一: 全库 DATETIME 列已统一 Python 本地时间,
    # 本端点两张表同一语义, 单一 cutoff (此前 capabilities_log=UTC /
    # usage_logs=本地 双轨, 需 cutoff_utc/cutoff_local 两个窗口)。
    now = datetime.now()
    cutoff = now - timedelta(days=days)

    # --- 1) pending_review 积压 (当前 + 每周新增) ---
    pending_total = (
        await session.execute(
            select(func.count(ModelCapabilitiesLog.id))
            .where(ModelCapabilitiesLog.approved == 0)
            .where(ModelCapabilitiesLog.source == "ai_infer")
        )
    ).scalar() or 0

    week_buckets = {
        "0-7d": (now - timedelta(days=7), now),
        "7-14d": (now - timedelta(days=14), now - timedelta(days=7)),
        "14d+": (datetime(2000, 1, 1), now - timedelta(days=14)),
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
            .where(ModelCapabilitiesLog.created_at >= cutoff)
        )
    ).scalar() or 0
    approved_count = (
        await session.execute(
            select(func.count(ModelCapabilitiesLog.id))
            .where(ModelCapabilitiesLog.source == "ai_infer")
            .where(ModelCapabilitiesLog.approved == 1)
            .where(ModelCapabilitiesLog.created_at >= cutoff)
        )
    ).scalar() or 0
    rejected_count = max(reviewed_count - approved_count, 0)

    # --- 4) 组路由命中 (usage_logs 按 group_id) ---
    group_rows = (
        await session.execute(
            select(UsageLog.group_id, func.count(UsageLog.id))
            .where(UsageLog.created_at >= cutoff)
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
            .where(UsageLog.created_at >= cutoff)
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
    # V6.5 (production-review S6): 周报原文含采集来的第三方数据 (模型名/URL 等),
    # 直接拼 HTML = 管理域存储型 XSS。escape 后前端 DOMParser 的 textContent
    # 会自动反转义回原文, marked 渲染链路零改动。
    return HTMLResponse(
        content=f"<pre class='report-md'>{html.escape(text)}</pre>",
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
    # V6.5 (production-review S6): 同 get_latest_report — 先 escape 防存储型 XSS
    return HTMLResponse(
        content=f"<pre class='report-md'>{html.escape(text)}</pre>",
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
        # V6.14: 暴露人工禁用时间 — 此前该列只在 DB 里, 前端/API 都看不到,
        # 「status 已 active 但 manual_disabled_at 残留」这类脱同步无从排查
        "manual_disabled_at": (
            account.manual_disabled_at.isoformat() if account.manual_disabled_at else None
        ),
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
    # V6.14: 非人工禁用态一律清 DB 时间戳, 与 /manual-enable 对齐 — 否则出现
    # status=active 但 manual_disabled_at 残留 (glm_01 生产实证的脱同步形态)
    if new_status != "manual_disabled":
        account.manual_disabled_at = None
    await session.commit()
    # V6.14: 内存冷却态与 DB 同步 — 此前本路径只写 DB, 账号表「启用」后内存
    # manual_disabled + 远期 cooldown 残留, 待处理仍显示「人工禁用」且路由
    # 继续排除 (glm_01 启用后僵死 4 天实证)。下面 refresh_cache 里另有对账
    # 兜底 (DB 永远赢), 这里显式同步是让语义不依赖 refresh 成败。
    from gateway.cooldown import cooldown_manager
    if new_status == "manual_disabled":
        cooldown_manager.mark_manual_disabled(account_id)
    else:
        cooldown_manager.clear_manual_disabled(account_id)
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
    # V6.11: allowed_groups 解析 (坏 JSON 原样透出, 写入口已校验, 正常不会发生)
    try:
        allowed_groups = json.loads(k.allowed_groups) if k.allowed_groups else None
    except (ValueError, TypeError):
        allowed_groups = k.allowed_groups
    return {
        "id": k.id,
        "key_prefix": k.key_prefix,
        "is_admin": bool(k.is_admin),
        "status": k.status,
        "label": k.label,
        "allowed_groups": allowed_groups,
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
        "notes": "产品组",                    # 可选, 用户备注
        "allowed_groups": ["tier:light"]     # 可选, V6.11 组白名单 (数组或逗号分隔
                                             # 字符串; NULL/空=不限; 组名须存在于 model_groups)
    }
    返回 201: { user_id, key_id, key_prefix, label, allowed_groups, key_plain }
    """
    import hashlib
    import secrets

    username = (payload.get("username") or "").strip()
    if not username:
        raise HTTPException(status_code=400, detail="username 必填")

    is_admin = bool(payload.get("admin", False))
    role = payload.get("role") or ("admin" if is_admin else "user")

    # V6.11: 组白名单归一化 + 校验 (组名必须存在于 model_groups, 防 typo 入库)
    allowed_groups = payload.get("allowed_groups")
    if allowed_groups is not None:
        if isinstance(allowed_groups, str):
            allowed_groups = [g.strip() for g in allowed_groups.split(",") if g.strip()]
        if not isinstance(allowed_groups, list) or not all(isinstance(g, str) and g for g in allowed_groups):
            raise HTTPException(status_code=400, detail="allowed_groups 须为组名字符串数组或逗号分隔字符串")
        if allowed_groups:
            existing_groups = set((await session.execute(
                select(ModelGroup.group_id)
            )).scalars().all())
            unknown = [g for g in allowed_groups if g not in existing_groups]
            if unknown:
                raise HTTPException(
                    status_code=400,
                    detail=f"未知组: {sorted(set(unknown))} (合法组: {sorted(existing_groups)})",
                )
        else:
            allowed_groups = None  # 空数组归一为不限

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
        allowed_groups=json.dumps(allowed_groups) if allowed_groups else None,
    )
    session.add(api_key)
    await session.commit()
    logger.info("V6.1 dashboard create key: user_id=%s username=%s key_id=%s admin=%s allowed_groups=%s",
                user.id, username, api_key.id, is_admin, allowed_groups)

    return {
        "user_id": user.id,
        "key_id": api_key.id,
        "key_prefix": api_key.key_prefix,
        "label": api_key.label,
        "allowed_groups": allowed_groups,
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
