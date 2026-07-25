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


async def forward_request(channel: dict, body: bytes, headers: Headers):
    """将请求透传到上游渠道

    Args:
        channel: 渠道信息字典，包含 base_url, api_key 等
        body: 原始请求体字节
        headers: 原始请求头

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

    target_url = f"{channel['base_url'].rstrip('/')}/chat/completions"

    start_time = time.time()

    try:
        if is_stream:
            return await _forward_stream(target_url, forward_headers, body, channel, start_time)
        else:
            return await _forward_normal(target_url, forward_headers, body, channel, start_time)
    except httpx.TimeoutException:
        logger.error(f"上游超时: {channel['name']} ({target_url})")
        return JSONResponse(
            status_code=504,
            content={"error": {"message": "Upstream timeout", "type": "gateway_timeout"}},
        )
    except httpx.ConnectError as e:
        logger.error(f"上游连接失败: {channel['name']} - {e}")
        return JSONResponse(
            status_code=502,
            content={"error": {"message": "Upstream connection failed", "type": "gateway_error"}},
        )
    except Exception as e:
        logger.error(f"转发异常: {channel['name']} - {e}")
        return JSONResponse(
            status_code=500,
            content={"error": {"message": "Internal gateway error", "type": "internal_error"}},
        )


async def _forward_stream(
    url: str, headers: dict, body: bytes, channel: dict, start_time: float
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

    return StreamingResponse(
        stream_generator(),
        status_code=response.status_code,
        headers=response_headers,
        media_type=response.headers.get("content-type", "text/event-stream"),
    )


async def _forward_normal(
    url: str, headers: dict, body: bytes, channel: dict, start_time: float
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

    Args:
        channel: 渠道信息 (含 name)
        response: 上游响应
    """
    status = response.status_code
    # 提取错误信息 (前 200 字符)
    try:
        err_body = response.text[:200]
    except Exception:
        err_body = "<unreadable>"

    msg = (
        f"[AIMC 网关告警] 上游 {channel.get('name', '?')} 返回 {status}\n"
        f"模型: {channel.get('model', '?')}\n"
        f"错误: {err_body}"
    )
    try:
        asyncio.get_event_loop().create_task(_send_alert_async(msg))
    except RuntimeError:
        # 没 event loop (e.g. 关闭时), 同步发
        import logging
        logging.getLogger(__name__).warning(f"[alerter fallback] {msg}")


async def _send_alert_async(message: str):
    """异步调 alerter.send_alert, 失败不阻塞"""
    try:
        from intelligence.alerter import send_alert
        await send_alert(message)
    except Exception as e:
        logger.warning(f"告警发送失败: {e}")
