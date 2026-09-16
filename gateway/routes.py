"""网关 API 端点定义

提供 OpenAI 兼容接口：
- /v1/chat/completions
- /v1/models
- /health
"""

import logging
import time

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


@router.get("/v1/models/{model_id}")
async def get_model(model_id: str):
    """V4.9.7: 单模型/单组详情 (OpenAI GET /v1/models/{id} 兼容)

    之前只有列表端点, 客户端探测 tier:strong 等组 id 时 404, 会误判模型不存在
    而静默切走. 复用 list_models 数据源, 命中组或精确 model 均返回 200.
    """
    for item in route_engine.get_models_for_openai_sdk(include_models=True):
        if item.get("id") == model_id:
            return item
    return JSONResponse(
        status_code=404,
        content={"error": {
            "message": f"The model '{model_id}' does not exist",
            "type": "invalid_request_error",
            "code": "model_not_found",
        }},
    )


@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """OpenAI 兼容的 chat completions 端点

    核心逻辑：
    1. 读取原始请求体
    2. 路由决策选择最优渠道
    3. 原样透传到上游（流式/非流式）
    """
    body = await request.body()
    _req_start = time.time()

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

    # V4.8.7 P7: 收集重试链中每个失败的渠道, 最终注入响应头让调用方定位余额不足渠道
    failed_channels = []

    for attempt in range(max_retries + 1):
        # P0-3 (2026-08-14): 传 messages 给 router, 让纯文本请求跳过 multimodal candidate
        if attempt == 0:
            channel = route_engine.select_channel(model, prefs=prefs, messages=body_json.get("messages"))
        else:
            # 重试: 重新选 (cooldown account 已被 router 跳过)
            await route_engine._auto_refresh_if_stale()
            channel = route_engine.select_channel(model, prefs=prefs, messages=body_json.get("messages"))

        # V4.9.7: 浅拷贝隔离 per-request 路由字段 (_channel_model/_registry_*/_group_id)
        # select_channel 返回的是 _channels_cache 里的共享 dict, 并发请求会互相覆盖这些
        # 字段 (2026-09-02 实测: strong 请求残留 _channel_model=minimax-m3 后, 随后的
        # balanced 请求 body 被替换成 minimax-m3 发往上游). prices/quotas 等列表只读, 共享无碍
        if channel is not None:
            channel = dict(channel)

        if channel is None:
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "message": f"No available channel for model: {model}",
                        "type": "service_unavailable",
                        "failed_channels": failed_channels,
                    }
                },
                headers={"X-AIMC-Failed-Channels": json.dumps(failed_channels, ensure_ascii=True)} if failed_channels else {},
            )

        # P0-6 (2026-08-14): 多维限额检查 (rpm/tpm/concurrent/daily/weekly/5h)
        # V4.8: 同时做 per-user 日/月 token 限额检查
        # V6.4.x (2026-09-14): 账号配额预检失败不再直接拒死请求. 旧行为是隐形
        # 429 (不 mark_failure / 不落 usage_logs / 不 failover), 生产实证:
        # deepseek_01 tpm 静态估算 (max_tokens×2 > 100000 恒真) 把 deepseek-flash
        # 兜底永久封死且看板不可见. 新行为: mark_failure 冷却该账号 (路由层跳过)
        # + 落 usage_logs + 组路由 failover 重选下一候选.
        # 用户级限额仍直接拒绝 (换渠道无济于事, 属调用方自身配额).
        if channel is not None:
            from gateway.quota import check_account_quota, check_user_quota
            from db.engine import async_session_factory
            caller_id = getattr(request.state, "caller_id", None)
            caller_key_id = getattr(request.state, "caller_key_id", None)
            async with async_session_factory() as qsession:
                # V6.6: 账号配额为纯真实测量 (usage_logs 窗口 SUM/COUNT + 内存并发),
                # 无 estimated_tokens 预留 — 旧静态门 max_tokens×2>limit 已根除
                allowed, reason = await check_account_quota(
                    qsession,
                    channel["account_id"],
                )
                if not allowed:
                    from gateway.cooldown import cooldown_manager
                    from gateway.error_classifier import ErrorCategory, ErrorInfo
                    from gateway.proxy import _write_usage_log
                    _quota_info = ErrorInfo(
                        category=ErrorCategory.QUOTA_EXHAUSTED,
                        error_code=None,
                        error_msg=reason,
                        needs_human=True,
                        suggested_cooldown_seconds=3600,
                        suggested_probe_after_seconds=None,
                    )
                    cooldown_manager.mark_failure(
                        channel["account_id"],
                        error_info=_quota_info,
                        sibling_account_ids=route_engine.sibling_account_ids(
                            channel["id"], channel["account_id"]
                        ),
                    )
                    failed_channels.append({
                        "channel": channel["name"],
                        "account": channel.get("account_name", ""),
                        "account_id": channel.get("account_id"),
                        "status": 429,
                        "error_type": _quota_info.category.value,
                        "error_code": None,
                        "error_msg": reason[:200],
                        "needs_human": True,
                    })
                    logger.warning(
                        f"配额预检拒绝: {channel['name']}"
                        f"/{channel.get('account_name', '')} {reason}, "
                        f"failover 到下一候选 (attempt {attempt + 1}/{max_retries + 1})"
                    )
                    # 失败落库 — 旧行为此路径无 usage_logs 行, 看板完全不可见
                    await _write_usage_log(
                        channel["id"], channel["account_id"], model,
                        input_tokens=0, output_tokens=0,
                        status_code=429,
                        latency_ms=int((time.time() - _req_start) * 1000),
                        group_id=channel.get("_group_id"),
                        caller_id=caller_id,
                        caller_key_id=caller_key_id,
                        error_msg=reason[:500],
                        error_type=_quota_info.category.value,
                        needs_human=True,
                    )
                    if attempt < max_retries:
                        continue
                    return JSONResponse(
                        status_code=429,
                        content={"error": {
                            "message": f"Quota exceeded: {reason}",
                            "type": "rate_limit_error",
                            "failed_channels": failed_channels,
                        }},
                        headers={
                            "retry-after": "60",
                            "X-AIMC-Failed-Channels": json.dumps(
                                failed_channels, ensure_ascii=False
                            ),
                            "X-AIMC-Last-Error-Type": _quota_info.category.value,
                        },
                    )

                # V4.8: per-user 日/月 token 限额 (caller_id=None 时跳过)
                if caller_id is not None:
                    u_allowed, u_reason = await check_user_quota(
                        qsession, caller_id,
                        estimated_tokens=(body_json.get("max_tokens") or 100) * 2,
                    )
                    if not u_allowed:
                        logger.warning(
                            f"用户配额拒绝: caller_id={caller_id} {u_reason}"
                        )
                        return JSONResponse(
                            status_code=429,
                            content={"error": {
                                "message": f"User quota exceeded: {u_reason}",
                                "type": "rate_limit_error",
                            }},
                            headers={"retry-after": "60"},
                        )

        # 透传请求到上游 (Phase 2.5: 传 requested_model 用于响应头 X-AIMC-Actual-Model)
        # V4.6.4: 传 channel._actual_model 作为 actual_model
        # V4.8: 传 caller_id (从 request.state, middleware 设的) 到 usage_logs
        # V4.8.1: 传 caller_key_id (api_keys.id) 到 usage_logs.key_id (多 key label 下钻)
        actual_model = channel.get("_actual_model") or model
        response = await forward_request(
            channel, body, request.headers,
            requested_model=model,
            actual_model=actual_model,
            caller_id=caller_id,
            caller_key_id=caller_key_id,
        )

        # P1-15: 非流式 + 429/5xx + 还有重试机会 -> 继续下一候选
        # P2-A: 组路由 (tier:/scene:) 的上游 400 也参与 failover
        # (场景: 选了不兼容的 member 导致 400, 换下一候选可能成功;
        #  精确模型路由的 400 是客户端请求格式错误, 重试无意义, 不 failover)
        is_group_route = model.startswith(("tier:", "scene:"))
        retryable_status = response.status_code in (429, 500, 502, 503, 504)
        if is_group_route and response.status_code == 400:
            retryable_status = True
        if (not is_stream) and retryable_status and attempt < max_retries:
            from gateway.error_classifier import classify
            # V4.9: 用 error_classifier 拿到 error_type/error_msg/needs_human
            # V6.4.x: 这里不再 mark_failure — proxy.forward_request 内部的
            # _update_cooldown 已标记过, 双重标记会把连续失败计数翻倍
            # (指数退避下首败冷却就会翻倍), 只保留分类供 failed_channels 记录
            try:
                _resp_body = response.text
            except Exception:
                _resp_body = None
            _error_info = classify(response.status_code, _resp_body, None)
            # V4.8.7 P7 + V4.9: 记录失败渠道 + 错误分类, 供最终响应注入
            failed_channels.append({
                "channel": channel["name"],
                "account": channel.get("account_name", ""),
                "account_id": channel.get("account_id"),
                "status": response.status_code,
                "error_type": _error_info.category.value,
                "error_code": _error_info.error_code,
                "error_msg": _error_info.error_msg[:200],
                "needs_human": _error_info.needs_human,
            })
            logger.warning(
                f"P1-15: {channel['name']} {response.status_code} "
                f"({_error_info.category.value}), "
                f"failover 到下一候选 (attempt {attempt + 1}/{max_retries})"
            )
            continue

        # V4.8.7 P7: 最后一次也失败 (或非 retryable), 把失败链注入响应头
        if failed_channels:
            try:
                content = response.body
                parsed = json.loads(content)
            except Exception:
                parsed = {"error": {"message": "Upstream error", "type": "upstream_error"}}
            if isinstance(parsed, dict) and "error" in parsed:
                parsed["error"]["failed_channels"] = failed_channels
            new_headers = dict(response.headers)
            # V6.5 (production-review S1): 复制的 headers 带原响应的 content-length
            # (及 hop-by-hop 头), 注入 failed_channels 后 body 变长而 starlette 检测到
            # 已有 content-length 不再重算 → 声明长度 < 实际长度, h11 断连或客户端
            # 截断 JSON (本机实证: 声明 25B / 实际 351B)。构造新响应前必须剥掉。
            for _h in ("content-length", "transfer-encoding", "connection"):
                new_headers.pop(_h, None)
            new_headers["X-AIMC-Failed-Channels"] = json.dumps(failed_channels, ensure_ascii=True)
            # V4.9: 加 X-AIMC-Last-Error-Type + Last-Error-Code, 客户端无需 parse JSON
            if failed_channels:
                last = failed_channels[-1]
                new_headers["X-AIMC-Last-Error-Type"] = last.get("error_type") or "unknown"
                if last.get("error_code"):
                    new_headers["X-AIMC-Last-Error-Code"] = str(last["error_code"])
                # 如果最后失败 needs_human, 加 Retry-After 提示人工
                if last.get("needs_human"):
                    new_headers["Retry-After"] = "3600"
            return JSONResponse(
                status_code=response.status_code,
                content=parsed,
                headers=new_headers,
            )

        return response

    # 兜底 (理论上到不了这, 但如果到说明所有重试都 continue 了)
    from fastapi.responses import JSONResponse as _JR
    return _JR(
        status_code=503,
        content={"error": {
            "message": "All upstream channels failed",
            "type": "service_unavailable",
            "failed_channels": failed_channels,
        }},
        headers={"X-AIMC-Failed-Channels": json.dumps(failed_channels, ensure_ascii=True)} if failed_channels else {},
    )
