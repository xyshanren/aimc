"""请求日志中间件 + 简单鉴权"""

import time
import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response, JSONResponse

from config import settings

logger = logging.getLogger(__name__)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """记录请求日志 + 可选 API Key 鉴权"""

    async def dispatch(self, request: Request, call_next) -> Response:
        start_time = time.time()

        # 简单鉴权（如果配置了 API Key）
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
