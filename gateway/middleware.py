"""请求日志中间件 + 多 key 鉴权 (V4.8) + Dashboard Token 鉴权

V4.8 (2026-08-19): 多用户鉴权改造
- 优先查 api_keys 表 (SHA256 hash 匹配), 命中后设 request.state.caller_id / is_admin
- api_keys 表为空或未建表时, 回退到 legacy 单 key 模式 (settings.api_key)
- last_used_at 异步更新 (fire-and-forget, 不阻塞请求)
- 带 30s 内存缓存 (key_hash -> ApiKeyInfo), 减少热路径 DB 查询
"""

import asyncio
import hashlib
import hmac
import time
import logging
from dataclasses import dataclass

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response, JSONResponse, RedirectResponse

from config import settings

logger = logging.getLogger(__name__)


# P1-7 (2026-08-14): dashboard 鉴权白名单
# 这些路径即使 dashboard_token 校验失败也允许访问
_DASHBOARD_AUTH_WHITELIST = (
    "/dashboard/login",       # login 页面 (无 token 时显示)
    "/dashboard/static/",     # 静态资源 (CSS/JS/chart.js)
    "/dashboard/api/auth/",   # 鉴权相关 API (login 提交, health)
)


# V4.8: 多用户鉴权缓存
# key_hash -> (timestamp, ApiKeyInfo | None), 30s TTL
# key revoke 后最长 30s 仍可用, 个人项目可接受
_APIKEY_CACHE_TTL = 30.0
_apikey_cache: dict[str, tuple[float, "ApiKeyInfo | None"]] = {}
_apikey_cache_last_purge = 0.0


@dataclass
class ApiKeyInfo:
    """V4.8: 鉴权命中的 key 信息 (middleware 写到 request.state)"""
    user_id: int
    username: str
    is_admin: bool
    key_id: int


def _extract_dashboard_token(request: Request) -> str:
    """P1-7: 从请求里提取 dashboard token, 支持 3 种方式

    优先级:
    1. Authorization: Bearer <token>
    2. Cookie: aimc_dashboard_token=<token>
    3. ?token=<token> (URL query)

    设计原因:
    - Authorization header: API-style 调用 (脚本/curl)
    - Cookie: 浏览器登录后保持 (避免每次输 token)
    - ?token=: 简单分享链接
    """
    # 1. Authorization header
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    # 2. Cookie
    cookie = request.cookies.get("aimc_dashboard_token", "")
    if cookie:
        return cookie
    # 3. Query param
    return request.query_params.get("token", "")


async def _lookup_api_key(token: str) -> ApiKeyInfo | None:
    """V4.8: 查 api_keys 表校验 token (带内存缓存)

    Returns:
        ApiKeyInfo (命中) / None (未命中或表不存在)

    缓存策略:
    - key_hash -> (ts, ApiKeyInfo | None), 30s TTL
    - 未命中的 hash 也缓存 (负缓存), 避免 revoked key 反复查 DB
    """
    key_hash = hashlib.sha256(token.encode()).hexdigest()

    # 查缓存 (命中, 包括负缓存)
    now = time.time()
    cached = _apikey_cache.get(key_hash)
    if cached is not None:
        ts, info = cached
        if now - ts < _APIKEY_CACHE_TTL:
            return info

    # 周期性清理过期项 (避免内存泄漏, 每 5 分钟清一次)
    global _apikey_cache_last_purge
    if now - _apikey_cache_last_purge > 300:
        expired = [k for k, (ts, _) in _apikey_cache.items() if now - ts > _APIKEY_CACHE_TTL]
        for k in expired:
            _apikey_cache.pop(k, None)
        _apikey_cache_last_purge = now

    # 查 DB
    info = await _query_api_key(key_hash)
    _apikey_cache[key_hash] = (now, info)
    return info


async def _query_api_key(key_hash: str) -> ApiKeyInfo | None:
    """V4.8: 从 api_keys 表查 key_hash -> user 信息

    表不存在或查询异常时返回 None (让 caller 回退到 legacy 模式)
    """
    try:
        from db.engine import async_session_factory
        from db.models import ApiKey, User
        from sqlalchemy import select

        async with async_session_factory() as session:
            stmt = (
                select(ApiKey, User)
                .join(User, ApiKey.user_id == User.id)
                .where(ApiKey.key_hash == key_hash)
                .where(ApiKey.status == "active")
                .where(User.status == "active")
            )
            result = await session.execute(stmt)
            row = result.first()
            if row is None:
                return None

            api_key: ApiKey = row[0]
            user: User = row[1]
            return ApiKeyInfo(
                user_id=user.id,
                username=user.username,
                is_admin=bool(api_key.is_admin) or user.role == "admin",
                key_id=api_key.id,
            )
    except Exception as e:
        logger.debug(f"V4.8 _query_api_key 异常 (可能表未建): {e}")
        return None


async def _update_key_last_used(key_id: int) -> None:
    """V4.8: 异步更新 api_keys.last_used_at (fire-and-forget)"""
    try:
        from datetime import datetime
        from db.engine import async_session_factory
        from db.models import ApiKey
        from sqlalchemy import update

        async with async_session_factory() as session:
            await session.execute(
                update(ApiKey)
                .where(ApiKey.id == key_id)
                .values(last_used_at=datetime.now())
            )
            await session.commit()
    except Exception as e:
        logger.debug(f"V4.8 _update_key_last_used 非致命: {e}")


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """记录请求日志 + 多 key 鉴权 (V4.8) + Dashboard Token 鉴权"""

    async def dispatch(self, request: Request, call_next) -> Response:
        start_time = time.time()

        # === P1-7: Dashboard 鉴权 (跟 api_key 分开) ===
        # - dashboard_token 未设 = 不鉴权 (legacy, 向后兼容)
        # - dashboard_token 设了 = /dashboard/* 路径需要 token 校验
        # - 跟 api_key 的区别: api_key 保护 /v1/*, dashboard_token 保护 /dashboard/*
        if settings.dashboard_auth_enabled and request.url.path.startswith("/dashboard"):
            # 白名单路径跳过鉴权 (login 页 + 静态资源)
            is_whitelisted = any(
                request.url.path.startswith(p) for p in _DASHBOARD_AUTH_WHITELIST
            )
            if not is_whitelisted:
                provided_token = _extract_dashboard_token(request)
                # 用 hmac.compare_digest 防止时序攻击
                if not hmac.compare_digest(provided_token, settings.dashboard_token):
                    # API 路径 -> 401 JSON
                    # HTML 路径 -> 302 重定向到 login
                    if request.url.path.startswith("/dashboard/api/"):
                        return JSONResponse(
                            status_code=401,
                            content={"error": {
                                "message": "Dashboard token required",
                                "type": "authentication_error",
                            }},
                        )
                    else:
                        login_url = "/dashboard/login?next=" + request.url.path
                        return RedirectResponse(url=login_url, status_code=302)

        # === V4.8: 多 key 鉴权 (/v1/* 等) ===
        # 跳过健康检查和 Dashboard / 文档路径
        if not request.url.path.startswith(("/health", "/dashboard", "/static", "/docs", "/openapi", "/redoc")):
            # 默认 caller_id = None (未鉴权 / legacy)
            request.state.caller_id = None
            request.state.caller_is_admin = False
            request.state.caller_username = None

            if settings.auth_enabled:
                auth_header = request.headers.get("Authorization", "")
                token = auth_header.replace("Bearer ", "") if auth_header else ""

                if not token:
                    return JSONResponse(
                        status_code=401,
                        content={"error": {"message": "Missing API key", "type": "authentication_error"}},
                    )

                # V4.8: 先查 api_keys 表 (多用户模式)
                key_info = await _lookup_api_key(token)

                if key_info is not None:
                    # 多用户命中: 设 caller 信息
                    request.state.caller_id = key_info.user_id
                    request.state.caller_is_admin = key_info.is_admin
                    request.state.caller_username = key_info.username
                    # 异步更新 last_used_at (不阻塞)
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(_update_key_last_used(key_info.key_id))
                    except RuntimeError:
                        pass
                elif hmac.compare_digest(token, settings.api_key):
                    # Legacy 回退: 单 key 模式 (settings.api_key)
                    # caller_id 保持 None (不归属任何 user, 按 legacy 统计)
                    logger.debug("V4.8: legacy api_key 命中 (api_keys 表无此 key)")
                else:
                    return JSONResponse(
                        status_code=401,
                        content={"error": {"message": "Invalid API key", "type": "authentication_error"}},
                    )

        # 执行请求
        response = await call_next(request)

        # 记录日志
        latency_ms = int((time.time() - start_time) * 1000)
        caller_tag = ""
        if getattr(request.state, "caller_username", None):
            caller_tag = f" user={request.state.caller_username}"
        logger.info(
            f"{request.method} {request.url.path} -> {response.status_code} ({latency_ms}ms){caller_tag}"
        )

        return response