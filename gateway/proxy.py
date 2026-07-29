"""核心代理转发逻辑

关键原则：绝不解析 SSE 流，原样透传字节。
- 流式请求：使用 aiter_raw() 返回原始字节流
- 非流式请求：直接返回上游 JSON 响应
"""

import json
import time
import logging
import asyncio

import httpx
from fastapi.responses import StreamingResponse, JSONResponse
from starlette.datastructures import Headers

from config import settings

logger = logging.getLogger(__name__)


def _build_aimc_headers(channel: dict, requested_model: str) -> dict:
    """Phase 2.5: 构造 X-AIMC-* 响应头

    三个 header:
    - X-AIMC-Actual-Model: 实际命中的 model (组路由时被替换, 精确路由时 = requested_model)
    - X-AIMC-Actual-Channel: 实际命中的 channel name (e.g. siliconflow_aggregator)
    - X-AIMC-Group-Id: 命中的 model group (仅组路由时有, e.g. tier:balanced)

    用途: Agent 知道"我的 tier:balanced 实际打到哪个 model + channel",
    方便 hermes-tray v0.3.0 footer pill + stats page 消费。
    """
    headers = {}
    # 优先用 _actual_model (Phase 2.4 _select_by_group 注入), fallback 到 requested_model
    actual_model = channel.get("_actual_model") or requested_model
    if actual_model:
        headers["X-AIMC-Actual-Model"] = actual_model
    if channel.get("name"):
        headers["X-AIMC-Actual-Channel"] = channel["name"]
    # group_id 仅组路由时存在, 精确路由时不发这个 header
    if channel.get("_group_id"):
        headers["X-AIMC-Group-Id"] = channel["_group_id"]
    return headers


async def forward_request(channel: dict, body: bytes, headers: Headers, requested_model: str = ""):
    """将请求透传到上游渠道

    Args:
        channel: 渠道信息字典，包含 base_url, api_key 等
        body: 原始请求体字节
        headers: 原始请求头
        requested_model: 请求的 model 字符串 (Phase 2.5 用于响应头 X-AIMC-Actual-Model)

    Returns:
        StreamingResponse 或 JSONResponse
    """
    # 构建上游请求头
    forward_headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {channel['api_key']}",
    }

    # 判断是否为流式请求
    try:
        body_json = json.loads(body)
        is_stream = body_json.get("stream", False)
    except (json.JSONDecodeError, UnicodeDecodeError):
        is_stream = False

    # Phase 2.5: 构造 X-AIMC-* 响应头 (在 group 路由时注入 _actual_model 等)
    aimc_headers = _build_aimc_headers(channel, requested_model)

    target_url = f"{channel['base_url'].rstrip('/')}/chat/completions"

    start_time = time.time()

    try:
        if is_stream:
            return await _forward_stream(
                target_url, forward_headers, body, channel, start_time, aimc_headers
            )
        else:
            return await _forward_normal(
                target_url, forward_headers, body, channel, start_time, aimc_headers
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
) -> JSONResponse:
    """非流式转发：等待完整响应后返回"""

    async with httpx.AsyncClient(timeout=httpx.Timeout(settings.request_timeout, connect=10.0)) as client:
        response = await client.post(url, headers=headers, content=body)

    latency_ms = int((time.time() - start_time) * 1000)
    logger.info(f"[Normal] {channel['name']} -> {response.status_code} ({latency_ms}ms)")

    # Phase 2E: 上游错误 (5xx + 401/403) 触发告警
    if response.status_code in (401, 403, 500, 502, 503, 504):
        _schedule_alert(channel, response)

    # 尝试记录用量（异步，不阻塞响应）
    _schedule_usage_log(channel, response, latency_ms)

    return JSONResponse(
        status_code=response.status_code,
        content=response.json() if response.headers.get("content-type", "").startswith("application/json") else {"raw": response.text},
        headers=aimc_headers or {},
    )


def _schedule_usage_log(channel: dict, response: httpx.Response, latency_ms: int):
    """异步记录用量日志（fire-and-forget）"""
    try:
        data = response.json()
        usage = data.get("usage", {})
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
):
    """写入用量日志到数据库"""
    try:
        from db.engine import async_session_factory
        from db.models import UsageLog

        async with async_session_factory() as session:
            log = UsageLog(
                channel_id=channel_id,
                account_id=account_id,
                model=model,
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
