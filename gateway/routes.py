"""网关 API 端点定义

提供 OpenAI 兼容接口：
- /v1/chat/completions
- /v1/models
- /health
"""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from gateway.proxy import forward_request
from gateway.router import route_engine

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/health")
async def health_check():
    """健康检查探针"""
    return {"status": "ok", "service": "aimc"}


@router.get("/v1/models")
async def list_models():
    """列出可用模型（聚合所有渠道的模型）"""
    models = route_engine.get_available_models()
    return {
        "object": "list",
        "data": [
            {"id": m, "object": "model", "owned_by": "aimc"}
            for m in models
        ],
    }


@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """OpenAI 兼容的 chat completions 端点

    核心逻辑：
    1. 读取原始请求体
    2. 路由决策选择最优渠道
    3. 原样透传到上游（流式/非流式）
    """
    body = await request.body()

    # 解析请求中的 model 字段（仅用于路由，不修改 body）
    import json
    try:
        body_json = json.loads(body)
    except json.JSONDecodeError:
        return JSONResponse(
            status_code=400,
            content={"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}},
        )

    model = body_json.get("model", "")
    if not model:
        return JSONResponse(
            status_code=400,
            content={"error": {"message": "model field is required", "type": "invalid_request_error"}},
        )

    # 路由决策：选择最优渠道
    channel = route_engine.select_best_channel(model)
    if channel is None:
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "message": f"No available channel for model: {model}",
                    "type": "service_unavailable",
                }
            },
        )

    # 透传请求到上游
    return await forward_request(channel, body, request.headers)
