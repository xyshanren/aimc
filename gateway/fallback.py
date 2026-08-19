"""故障转移 + 重试逻辑

当首选渠道失败时，自动切换到下一个候选渠道。
最多重试 max_fallback_retries 次。
"""

import json
import logging

from fastapi import Request
from fastapi.responses import JSONResponse

from config import settings
from gateway.proxy import forward_request
from gateway.router import route_engine

logger = logging.getLogger(__name__)


async def forward_with_fallback(request: Request, body: bytes, model: str):
    """带故障转移的请求转发

    尝试候选渠道列表，直到成功或全部失败。

    Args:
        request: 原始 FastAPI 请求
        body: 原始请求体
        model: 请求的模型名

    Returns:
        上游响应或错误响应
    """
    candidates = route_engine.get_candidates(model)

    if not candidates:
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "message": f"No available channel for model: {model}",
                    "type": "service_unavailable",
                }
            },
        )

    max_retries = min(settings.max_fallback_retries, len(candidates) - 1)
    last_error = None

    for attempt, channel in enumerate(candidates[: max_retries + 1]):
        try:
            # V4.8: 传 caller_id (从 request.state, middleware 设的) 到 usage_logs
            caller_id = getattr(request.state, "caller_id", None)
            response = await forward_request(
                channel, body, request.headers, caller_id=caller_id,
            )

            # 检查是否为错误响应（502/503/504 触发 fallback）
            if hasattr(response, "status_code") and response.status_code in (502, 503, 504):
                logger.warning(
                    f"渠道 {channel['name']} 返回 {response.status_code}，尝试 fallback "
                    f"(attempt {attempt + 1}/{max_retries + 1})"
                )
                last_error = response
                continue

            return response

        except Exception as e:
            logger.warning(
                f"渠道 {channel['name']} 异常: {e}，尝试 fallback "
                f"(attempt {attempt + 1}/{max_retries + 1})"
            )
            last_error = e
            continue

    # 所有候选都失败
    logger.error(f"所有候选渠道均失败: model={model}, attempts={max_retries + 1}")

    if isinstance(last_error, Exception):
        return JSONResponse(
            status_code=502,
            content={
                "error": {
                    "message": f"All channels failed for model: {model}",
                    "type": "gateway_error",
                    "detail": str(last_error),
                }
            },
        )

    return last_error
