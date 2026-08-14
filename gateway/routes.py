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
    """列出可用模型 (Phase 3.B 增强, 加 aimc_metadata)

    演进历史:
    - Phase 2.8: 加上 data_groups 字段
    - Phase 3.B: 每个 model 加 aimc_metadata (family/tier/capabilities/...)
      每个 group 加 aimc_metadata (fallback_chain/member_tiers/...)

    向后兼容: OpenAI 客户端只读 data[].id, AIMC 扩展字段全部在 aimc_metadata 嵌套对象里

    用途:
    - hermes-agent-cn 启动时拉一次, 验证 profile 合法
    - 看 model 元信息, 不需额外查 model registry
    - 看 model 在哪些 channel 可用
    """
    return {
        "object": "list",
        "data": route_engine.get_models_with_metadata(),
        "data_groups": route_engine.get_groups_with_metadata(),
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

    # Phase 2.6: 解析 Agent 偏好 (extra_body.aimc_preferences)
    # 3 个字段: prefer_family / exclude_providers / max_input_price
    # 只对组路由生效 (精确模型路由 Phase 2 不实现)
    prefs = {}
    extra_body = body_json.get("extra_body") or {}
    if isinstance(extra_body, dict):
        aimc_prefs = extra_body.get("aimc_preferences") or {}
        if isinstance(aimc_prefs, dict):
            prefs = {
                k: v for k, v in aimc_prefs.items()
                if k in ("prefer_family", "exclude_providers", "max_input_price")
            }

    # 路由决策: select_channel (Phase 2.4 入口, 精确模型 + 模型组)
    # - "deepseek-chat" → 走原精确模型路由
    # - "tier:balanced" / "scene:code" → 走组路由 + fallback chain
    # P0-4 2026-08-14: 路由前先 auto-refresh stale cache
    # 避免 db 改了必须 systemctl restart 的运维陷阱
    await route_engine._auto_refresh_if_stale()
    channel = route_engine.select_channel(model, prefs=prefs)
    # P0-6 2026-08-14: 多维限额检查 (rpm/tpm/concurrent/daily/weekly/5h)
    # post-check: 选了 channel 后判断. 后续可改成 router 内候选过滤
    if channel is not None:
        from gateway.quota import check_account_quota
        from db.engine import async_session_factory
        async with async_session_factory() as qsession:
            allowed, reason = await check_account_quota(
                qsession,
                channel["account_id"],
                estimated_tokens=(body_json.get("max_tokens") or 100) * 2,
            )
            if not allowed:
                return JSONResponse(
                    status_code=429,
                    content={"error": {
                        "message": f"Quota exceeded: {reason}",
                        "type": "rate_limit_error",
                    }},
                    headers={"retry-after": "60"},
                )
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

    # 透传请求到上游 (Phase 2.5: 传 requested_model 用于响应头 X-AIMC-Actual-Model)
    # V4.6.4: 传 channel._actual_model 作为 actual_model
    #   - 精确路由时 = requested_model (无替换)
    #   - fallback 路由时 = channel 支持的 model (proxy 层会替换 body.model)
    actual_model = channel.get("_actual_model") or model
    return await forward_request(
        channel, body, request.headers,
        requested_model=model,
        actual_model=actual_model,
    )
