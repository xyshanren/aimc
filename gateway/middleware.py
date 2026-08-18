"""请求日志中间件 + 简单鉴权 + Dashboard Token 鉴权"""

import hmac
import time
import logging

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


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """记录请求日志 + 可选 API Key 鉴权 + 可选 Dashboard Token 鉴权"""

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

        # === 简单鉴权 (API Key, /v1/* 等) ===
        if settings.auth_enabled:
            # 跳过健康检查和 Dashboard 路径
            if not request.url.path.startswith(("/health", "/dashboard", "/static", "/docs", "/openapi")):
                auth_header = request.headers.get("Authorization", "")
                token = auth_header.replace("Bearer ", "") if auth_header else ""
                if token != settings.api_key:
                    return JSONResponse(
                        status_code=401,
                        content={"error": {"message": "Invalid API key", "type": "authentication_error"}},
                    )

        # 执行请求
        response = await call_next(request)

        # 记录日志
        latency_ms = int((time.time() - start_time) * 1000)
        logger.info(
            f"{request.method} {request.url.path} -> {response.status_code} ({latency_ms}ms)"
        )

        return response
