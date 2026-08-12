"""核心代理转发逻辑

关键原则：绝不解析 SSE 流，原样透传字节。
- 流式请求：使用 aiter_raw() 返回原始字节流
- 非流式请求：直接返回上游 JSON 响应
"""

import json
import time
import logging
import asyncio
from typing import Optional

import httpx
from fastapi.responses import StreamingResponse, JSONResponse
from starlette.datastructures import Headers

from config import settings

logger = logging.getLogger(__name__)


# V4.6.1: 视为上游失败的 status_code (跟现有 _schedule_alert 一致)
_FAILURE_STATUS_CODES = frozenset((401, 403, 500, 502, 503, 504))


def _update_cooldown(channel: dict, status_code: int) -> None:
    """V4.6.1: 根据 status_code 更新账号 cooldown 状态.

    - 2xx (成功) → mark_success: 清空失败计数 + 冷却
    - 401/403/5xx (失败) → mark_failure: 累计失败, 达到阈值进入冷却
    - 其他 (3xx/4xx 其他) → 不动 (e.g. 400 是请求方问题, 不应让账号冷却)

    调用点是 _forward_stream / _forward_normal 拿到 status_code 后立刻调用,
    不阻塞主响应, 不抛异常 (失败仅 log).
    """
    try:
        account_id = channel.get("account_id")
        if account_id is None:
            return
        from gateway.cooldown import cooldown_manager
        if status_code in _FAILURE_STATUS_CODES:
            cooldown_manager.mark_failure(
                account_id, reason=f"HTTP {status_code}"
            )
        elif 200 <= status_code < 300:
            cooldown_manager.mark_success(account_id)
    except Exception as e:  # 永不阻断主流程
        logger.warning(f"_update_cooldown 异常 (忽略): {e}")


def _build_aimc_headers(channel: dict, requested_model: str) -> dict:
    """Phase 3.E: 构造 X-AIMC-* 响应头

    五个 header (Phase 2.5 加 3 个, Phase 3.E 加 2 个):
    - X-AIMC-Actual-Model: 实际命中的 model (组路由时被替换, 精确路由时 = requested_model)
    - X-AIMC-Actual-Channel: 实际命中的 channel name (e.g. siliconflow_aggregator)
    - X-AIMC-Group-Id: 命中的 model group (仅组路由时有, e.g. tier:balanced)
    - X-AIMC-Actual-Tier: 实际命中 model 的能力档 (e.g. T2, 仅当 model 在 registry 时有)
    - X-AIMC-Actual-Capabilities: 实际命中 model 的 capabilities (JSON 字符串,
      e.g. '["reasoning","code"]', 仅当 model 在 registry 时有)

    用途: hermes-agent-cn 知道"我的 tier:balanced 实际打到哪个 model + channel",
    以及该 model 的能力档 / 能力标签, 方便 hermes-tray (通过 hermes-agent-cn)
    在 footer pill + stats page 展示。

    注意 (2026-07-29 Phase 2.10 E2E 发现):
    - _select_by_group 注入的 _actual_model / _group_id 是写在共享 channel dict 里
      (select_best_channel 返回的是 _channels_cache 里的对象, 不是 copy)
    - 后续精确路由命中**同一个 channel dict** 时, 这些字段仍残留, 污染响应头
    - 修法: 只在请求带 "tier:" / "scene:" 前缀时才信任 _actual_model
    - Phase 3.E 进一步: select_best_channel 返回前也写 _actual_model,
      所以精确路由时 _actual_model = requested_model 也成立 (不污染)
    """
    headers = {}
    is_group_request = requested_model.startswith(("tier:", "scene:"))
    if is_group_request and channel.get("_actual_model"):
        # 组路由: 用 _actual_model (Phase 2.4 注入)
        actual_model = channel["_actual_model"]
    else:
        # 精确路由: 用 requested_model (忽略 channel 里可能残留的 _actual_model 污染)
        actual_model = requested_model
    if actual_model:
        headers["X-AIMC-Actual-Model"] = actual_model
    if channel.get("name"):
        headers["X-AIMC-Actual-Channel"] = channel["name"]
    # group_id: 只在组路由请求 + channel 有 _group_id 时发
    if is_group_request and channel.get("_group_id"):
        headers["X-AIMC-Group-Id"] = channel["_group_id"]
    # Phase 3.E: tier / capabilities (从 channel._registry_* 读, 已在 router.py 注入)
    # tier: 简单字符串, 直接发
    if channel.get("_registry_tier"):
        headers["X-AIMC-Actual-Tier"] = channel["_registry_tier"]
    # capabilities: DB 存的是 JSON 字符串, 需要解析后重新序列化保证格式
    # (避免 "[\"reasoning\"]" 这种双重转义问题, 直接给 hermes-agent-cn 一个干净 JSON)
    caps_raw = channel.get("_registry_capabilities")
    if caps_raw:
        try:
            caps_list = json.loads(caps_raw)
            # 列表才有意义, 字典/字符串 fallback 成空
            if isinstance(caps_list, list):
                headers["X-AIMC-Actual-Capabilities"] = json.dumps(caps_list, ensure_ascii=False)
            else:
                headers["X-AIMC-Actual-Capabilities"] = "[]"
        except (ValueError, TypeError):
            headers["X-AIMC-Actual-Capabilities"] = "[]"
    return headers


async def forward_request(channel: dict, body: bytes, headers: Headers, requested_model: str = "", actual_model: Optional[str] = None):
    """将请求透传到上游渠道

    Args:
        channel: 渠道信息字典，包含 base_url, api_key 等
        body: 原始请求体字节
        headers: 原始请求头
        requested_model: 请求的 model 字符串 (用户原始请求, 用于响应头 X-AIMC-Actual-Model)
        actual_model: V4.6.4+ — router fallback 路径选择的 channel 模型名
                      (可能跟 requested_model 不同, 此时会替换 body.model)

    Returns:
        StreamingResponse 或 JSONResponse
    """
    # V4.6.4: fallback 路径 router 选了 channel 但 model 名跟用户请求的不同
    # 例: 用户请求 deepseek-v2.5, channel 只有 deepseek-ai/DeepSeek-V3
    # 替换 body.model = channel 支持的 model, 上游能识别
    actual_model = actual_model or requested_model
    if actual_model != requested_model:
        try:
            import json as _json_mod
            body_json = _json_mod.loads(body)
            if body_json.get("model") != actual_model:
                old_model = body_json.get("model")
                body_json["model"] = actual_model
                body = _json_mod.dumps(body_json, ensure_ascii=False).encode("utf-8")
                logger.info(
                    f"[V4.6.4 fallback model 替换] {old_model} -> {actual_model} "
                    f"(channel={channel.get('name')})"
                )
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.warning(f"body 解析失败, 跳过 model 替换: {e}")

    # 构建上游请求头
    forward_headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {channel['api_key']}",
    }
    # 本地模型 (Ollama / vllm / LM Studio / llama.cpp):
    # - Ollama 完全忽略 Authorization, 多发一个 header 也不影响
    # - vllm 严格: 多余的 "Bearer no-key-required" 可能被当作无效 token 拒绝
    # - LM Studio: 同 Ollama, 容忍
    # 稳妥做法: provider_type=local 时跳过 Authorization header
    if channel.get("provider_type") == "local":
        forward_headers.pop("Authorization", None)

    # 判断是否为流式请求
    try:
        body_json = json.loads(body)
        is_stream = body_json.get("stream", False)
    except (json.JSONDecodeError, UnicodeDecodeError):
        is_stream = False

    # Phase 2.5: 构造 X-AIMC-* 响应头 (在 group 路由时注入 _actual_model 等)
    # V4.6.4: 传 actual_model 让 _build_aimc_headers 用 fallback 后的真实 model 名
    aimc_headers = _build_aimc_headers(channel, actual_model)

    target_url = f"{channel['base_url'].rstrip('/')}/chat/completions"

    start_time = time.time()

    try:
        if is_stream:
            return await _forward_stream(
                target_url, forward_headers, body, channel, start_time,
                aimc_headers, requested_model=requested_model,
            )
        else:
            return await _forward_normal(
                target_url, forward_headers, body, channel, start_time,
                aimc_headers, requested_model=requested_model,
            )
    except httpx.TimeoutException:
        logger.error(f"上游超时: {channel['name']} ({target_url})")
        return JSONResponse(
            status_code=504,
            content={"error": {"message": "Upstream timeout", "type": "gateway_timeout"}},
            headers=aimc_headers,
        )
    except httpx.ConnectError as e:
        logger.error(f"上游连接失败: {channel['name']} - {e}")
        return JSONResponse(
            status_code=502,
            content={"error": {"message": "Upstream connection failed", "type": "gateway_error"}},
            headers=aimc_headers,
        )
    except Exception as e:
        logger.error(f"转发异常: {channel['name']} - {e}")
        return JSONResponse(
            status_code=500,
            content={"error": {"message": "Internal gateway error", "type": "internal_error"}},
            headers=aimc_headers,
        )


async def _forward_stream(
    url: str, headers: dict, body: bytes, channel: dict, start_time: float,
    aimc_headers: dict | None = None,
    requested_model: str = "",
) -> StreamingResponse:
    """流式透传：原样传递字节，不解析 SSE"""

    client = httpx.AsyncClient(timeout=httpx.Timeout(settings.request_timeout, connect=10.0))

    req = client.build_request(
        method="POST",
        url=url,
        headers=headers,
        content=body,
    )

    response = await client.send(req, stream=True)

    # Phase 2E: 上游错误 (5xx + 401/403) 触发告警
    if response.status_code in (401, 403, 500, 502, 503, 504):
        _schedule_alert(channel, response)

    # V4.6.1: 更新账号 cooldown (失败进入冷却, 成功清空)
    _update_cooldown(channel, response.status_code)

    async def stream_generator():
        try:
            async for chunk in response.aiter_raw():
                yield chunk
        finally:
            await response.aclose()
            await client.aclose()
            latency_ms = int((time.time() - start_time) * 1000)
            logger.info(
                f"[Stream] {channel['name']} -> {response.status_code} ({latency_ms}ms)"
            )

    # 透传上游响应头（过滤掉 hop-by-hop 头）
    response_headers = {}
    for key, value in response.headers.items():
        if key.lower() not in ("transfer-encoding", "connection", "content-length"):
            response_headers[key] = value
    # Phase 2.5: 注入 X-AIMC-* 头 (覆盖同名 from upstream, 保证 client 看到的是 AIMC 实际命中)
    if aimc_headers:
        response_headers.update(aimc_headers)

    return StreamingResponse(
        stream_generator(),
        status_code=response.status_code,
        headers=response_headers,
        media_type=response.headers.get("content-type", "text/event-stream"),
    )


async def _forward_normal(
    url: str, headers: dict, body: bytes, channel: dict, start_time: float,
    aimc_headers: dict | None = None,
    requested_model: str = "",
) -> JSONResponse:
    """非流式转发：等待完整响应后返回

    V4.6.1: 接收 requested_model 透传给 _schedule_usage_log (Phase 3.A group_id 提取)
    """

    async with httpx.AsyncClient(timeout=httpx.Timeout(settings.request_timeout, connect=10.0)) as client:
        response = await client.post(url, headers=headers, content=body)

    latency_ms = int((time.time() - start_time) * 1000)
    logger.info(f"[Normal] {channel['name']} -> {response.status_code} ({latency_ms}ms)")

    # Phase 2E: 上游错误 (5xx + 401/403) 触发告警
    if response.status_code in (401, 403, 500, 502, 503, 504):
        _schedule_alert(channel, response)

    # V4.6.1: 更新账号 cooldown (失败进入冷却, 成功清空)
    _update_cooldown(channel, response.status_code)

    # 尝试记录用量（异步，不阻塞响应）
    _schedule_usage_log(channel, response, latency_ms, requested_model)

    return JSONResponse(
        status_code=response.status_code,
        content=response.json() if response.headers.get("content-type", "").startswith("application/json") else {"raw": response.text},
        headers=aimc_headers or {},
    )


def _extract_group_id(requested_model: str) -> str | None:
    """Phase 3.A: 从 requested_model 提取 group_id

    - tier:xxx / scene:xxx 前缀 → 返回 group_id (e.g. "tier:balanced")
    - 精确模型名 → 返回 None (不需要记录 group)
    """
    if requested_model and requested_model.startswith(("tier:", "scene:")):
        return requested_model
    return None


def _schedule_usage_log(channel: dict, response: httpx.Response, latency_ms: int, requested_model: str = ""):
    """异步记录用量日志（fire-and-forget）

    Phase 3.A: 接收 requested_model, 提取 group_id 写入 usage_logs
    - 精确模型路由: group_id = NULL
    - 组路由: group_id = "tier:xxx" / "scene:xxx"
    """
    try:
        data = response.json()
        usage = data.get("usage", {})
        group_id = _extract_group_id(requested_model)
        # 创建异步任务写入数据库
        asyncio.get_event_loop().create_task(
            _write_usage_log(
                channel_id=channel.get("id"),
                account_id=channel.get("account_id"),
                model=data.get("model", ""),
                input_tokens=usage.get("prompt_tokens", 0),
                output_tokens=usage.get("completion_tokens", 0),
                status_code=response.status_code,
                latency_ms=latency_ms,
                group_id=group_id,
            )
        )
    except Exception:
        pass  # 用量记录失败不影响主流程


async def _write_usage_log(
    channel_id: int | None,
    account_id: int | None,
    model: str,
    input_tokens: int,
    output_tokens: int,
    status_code: int,
    latency_ms: int,
    group_id: str | None = None,
):
    """写入用量日志到数据库

    Phase 3.A: group_id 参数
    - 精确模型路由时为 None
    - 组路由时为 "tier:xxx" / "scene:xxx"
    - 供 Dashboard 命中率聚合 GROUP BY group_id, model
    """
    try:
        from db.engine import async_session_factory
        from db.models import UsageLog

        async with async_session_factory() as session:
            log = UsageLog(
                channel_id=channel_id,
                account_id=account_id,
                model=model,
                group_id=group_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                status_code=status_code,
                latency_ms=latency_ms,
            )
            session.add(log)
            await session.commit()
    except Exception as e:
        logger.warning(f"用量日志写入失败: {e}")


def _schedule_alert(channel: dict, response: httpx.Response):
    """Phase 2E: 上游错误时触发告警 (fire-and-forget, 不阻塞响应)

    触发条件: 5xx (上游挂) + 401/403 (key 失效)
    不触发: 200 (成功) / 4xx 业务错误 (如 400 prompt error) / 429 (rate limit, 临时的)

    Phase 3 (#3): throttle key = "{channel_name}:{status}"
    - 同 channel + 同 status 60s 内只发 1 次告警
    - 防止硅基流动 1 key 反复 401 时飞书刷屏

    Args:
        channel: 渠道信息 (含 name)
        response: 上游响应
    """
    status = response.status_code
    channel_name = channel.get("name", "?")
    # 提取错误信息 (前 200 字符)
    try:
        err_body = response.text[:200]
    except Exception:
        err_body = "<unreadable>"

    msg = (
        f"[AIMC 网关告警] 上游 {channel_name} 返回 {status}\n"
        f"模型: {channel.get('model', '?')}\n"
        f"错误: {err_body}"
    )
    # throttle key: 同 channel + 同 status 共用, 不同 status (401 vs 500) 独立告警
    throttle_key = f"{channel_name}:{status}"
    try:
        asyncio.get_event_loop().create_task(
            _send_alert_async(msg, key=throttle_key)
        )
    except RuntimeError:
        # 没 event loop (e.g. 关闭时), 同步发
        import logging
        logging.getLogger(__name__).warning(f"[alerter fallback] {msg}")


async def _send_alert_async(message: str, key: str | None = None):
    """异步调 alerter.send_alert, 失败不阻塞

    Args:
        message: 告警文本
        key: throttle key (Phase 3 #3), 透传给 send_alert
    """
    try:
        from intelligence.alerter import send_alert
        await send_alert(message, key=key)
    except Exception as e:
        logger.warning(f"告警发送失败: {e}")
