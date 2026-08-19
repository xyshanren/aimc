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
from config import settings

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/health")
async def health_check():
    """健康检查探针"""
    return {"status": "ok", "service": "aimc"}


@router.get("/v1/models")
async def list_models(include_models: bool = False):
    """V4.6.4+: 列出可用模型 (OpenAI SDK 兼容)

    默认模式 (用户偏好):
    - 只返回 group (每个 group 当成 OpenAI model)
    - OpenAI SDK 严格模式接受 tier:xxx, 不再 "model not found"
    - agent / DSH / 任何 OpenAI 客户端直接用 tier:flagship / scene:code 等

    Query 参数:
    - include_models=true: 同时返回精确 model (admin / 调试用)
      URL: /v1/models?include_models=true

    之前 (V4.6.3 之前): 只返回精确 model, agent 调 tier:xxx 报 "model not in list"

    响应:
    {
      "object": "list",
      "data": [
        {"id": "tier:flagship", "object": "model", "owned_by": "aimc",
         "aimc_metadata": {"type": "group", "tier": "T1", "members": [...], ...}},
        {"id": "deepseek-v4-pro", "object": "model", "owned_by": "deepseek",
         "aimc_metadata": {"type": "model", "tier": "T1", ...}},  # include_models=true 时
      ],
      "data_groups": [...],  # 兼容旧字段 (dashboard 用)
    }
    """
    return {
        "object": "list",
        "data": route_engine.get_models_for_openai_sdk(include_models=include_models),
        "data_groups": route_engine.get_groups_with_metadata(),  # 兼容旧字段
        "include_models": include_models,
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
    # P0-4 (2026-08-14): 路由前先 auto-refresh stale cache
    # - 避免 "db 改了必须 systemctl restart" 的运维陷阱
    # - TTL (默认 60s) + db mtime 监听, db 变更立即生效
    await route_engine._auto_refresh_if_stale()
    # P1-15 (2026-08-15): 组路由失败自动 failover
    # 场景: minimax TokenPlan 429 (quota 耗尽) / 上游 5xx 时,
    #       mark_failure (proxy 层) 后 router 会跳过 cooldown account,
    #       重新 select_channel 得到下一个候选, 重试
    # 限制:
    # - 只对非流式重试 (流式 SSE 已开始不能安全重发)
    # - 最多 max_fallback_retries 次 (config.py, 默认 2)
    # - 精确模型路由 (非 tier:) 只试 1 次 (fallback 到聚合平台由 router 内部处理)
    is_stream = bool(body_json.get("stream", False))
    max_retries = settings.max_fallback_retries if model.startswith(("tier:", "scene:")) else 0

    for attempt in range(max_retries + 1):
        # P0-3 (2026-08-14): 传 messages 给 router, 让纯文本请求跳过 multimodal candidate
        if attempt == 0:
            channel = route_engine.select_channel(model, prefs=prefs, messages=body_json.get("messages"))
        else:
            # 重试: 重新选 (cooldown account 已被 router 跳过)
            await route_engine._auto_refresh_if_stale()
            channel = route_engine.select_channel(model, prefs=prefs, messages=body_json.get("messages"))

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

        # P0-6 (2026-08-14): 多维限额检查 (rpm/tpm/concurrent/daily/weekly/5h)
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

        # 透传请求到上游 (Phase 2.5: 传 requested_model 用于响应头 X-AIMC-Actual-Model)
        # V4.6.4: 传 channel._actual_model 作为 actual_model
        # V4.8: 传 caller_id (从 request.state, middleware 设的) 到 usage_logs
        actual_model = channel.get("_actual_model") or model
        caller_id = getattr(request.state, "caller_id", None)
        response = await forward_request(
            channel, body, request.headers,
            requested_model=model,
            actual_model=actual_model,
            caller_id=caller_id,
        )

        # P1-15: 非流式 + 429/5xx + 还有重试机会 -> 继续下一候选
        if (not is_stream) and response.status_code in (429, 500, 502, 503, 504) and attempt < max_retries:
            from gateway.cooldown import cooldown_manager
            cooldown_manager.mark_failure(
                channel["account_id"], reason=f"upstream_{response.status_code}"
            )
            logger.warning(
                f"P1-15: {channel['name']} {response.status_code}, "
                f"failover 到下一候选 (attempt {attempt + 1}/{max_retries})"
            )
            continue

        return response

    # 兜底 (理论上到不了这)
    from fastapi.responses import JSONResponse as _JR
    return _JR(
        status_code=503,
        content={"error": {"message": "All upstream channels failed", "type": "service_unavailable"}},
    )
