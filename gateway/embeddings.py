"""Embeddings / Rerank 直通端点

背景: siliconflow 渠道的 embedding/rerank 专用模型 (BAAI/bge-m3,
Qwen/Qwen3-Embedding 等) 不进组路由 (非对话格式, 组比价无意义),
forward_request 又硬编码 /chat/completions 无法复用, 所以提供独立直通:

- POST /v1/embeddings → {sf_base}/embeddings   (OpenAI 兼容)
- POST /v1/rerank     → {sf_base}/rerank       (Jina 兼容)

设计:
- 只走 siliconflow 渠道 (单账号直连, 不做组 failover)
- 上游模型名从 model_registry.raw_ids["siliconflow"] 反查
- 用量/费用照常入 usage_logs (复用 proxy._write_usage_log / _estimate_cost)
- 出站 URL 校验: 仅 http/https, 拒绝 localhost/环回/私有/保留地址
- 鉴权由 middleware 继承 (api_key 必带, caller_id/caller_key_id 自动透传)
"""

import asyncio
import ipaddress
import json
import logging
import time
from typing import Optional
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from config import settings
from gateway.proxy import (
    _estimate_cost,
    _estimate_list_cost,
    _log_task_result,
    _resolve_effective_base_url,
    _write_usage_log,
)
from gateway.router import route_engine

logger = logging.getLogger(__name__)

router = APIRouter()

_SF_PROVIDER = "siliconflow"
_RETRYABLE_STATUS = (429, 500, 502, 503, 504)


def _error(message: str, status: int, err_type: str = "invalid_request_error",
           code: Optional[str] = None) -> JSONResponse:
    content = {"error": {"message": message, "type": err_type}}
    if code:
        content["error"]["code"] = code
    return JSONResponse(status_code=status, content=content)


def _validate_upstream_url(url: str) -> bool:
    """出站 URL 校验: 仅 http/https; 拒绝 localhost/环回/私有/保留地址"""
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False
    if host == "localhost" or host.endswith(".localhost") or host == "0.0.0.0":
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True  # 普通域名
    return not (
        ip.is_loopback or ip.is_private or ip.is_reserved
        or ip.is_link_local or ip.is_multicast or ip.is_unspecified
    )


def _pick_sf_channel() -> Optional[dict]:
    """从 channels cache 选一个可用的 siliconflow 账号"""
    from gateway.cooldown import cooldown_manager

    for ch in route_engine._channels_cache:
        if ch.get("provider") != _SF_PROVIDER or not ch.get("api_key"):
            continue
        if cooldown_manager.is_in_cooldown(ch["account_id"]) \
                or cooldown_manager.is_manual_disabled(ch["account_id"]):
            continue
        return ch
    return None


def _resolve_upstream_name(model: str) -> Optional[str]:
    """网关规范名 → siliconflow 上游原始名 (raw_ids 反查)

    registry cache 存 active ModelRegistry ORM 对象, raw_ids 形如
    {"siliconflow": "BAAI/bge-m3"} (key 是 provider 名).
    """
    reg = route_engine._registry_cache.get(model)
    if reg is None:
        return None
    raw = reg.raw_ids
    # raw_ids 是 TEXT 列, SQLAlchemy 不解析, ORM 返回 JSON 字符串
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            raw = {}
    if isinstance(raw, dict):
        return raw.get(_SF_PROVIDER)
    return None


async def _prepare(model: str):
    """解析上游名 + 选渠道. 返回 (channel, upstream_name, error_response)"""
    # P0-4: 路由前 auto-refresh stale cache (与 chat 端点一致)
    await route_engine._auto_refresh_if_stale()
    channel = _pick_sf_channel()
    if channel is None:
        return None, None, _error(
            "No available siliconflow channel for embeddings/rerank",
            503, "service_unavailable",
        )

    upstream_name = _resolve_upstream_name(model)
    if not upstream_name:
        # 兼容直接传上游原始名 (如 "BAAI/bge-m3"): 价格行精确命中即放行
        if not any(p.get("model") == model for p in channel.get("prices", [])):
            return None, None, _error(
                f"The model '{model}' does not exist", 404,
                code="model_not_found",
            )
        upstream_name = model
    return channel, upstream_name, None


def _extract_tokens(data: dict) -> tuple:
    """从响应提取 token 数

    embeddings (OpenAI): usage.prompt_tokens / usage.completion_tokens
    rerank (Jina 兼容): usage.total_tokens / tokens / tokens.input_tokens
    多模态 (VL-Embedding/VL-Reranker): image_tokens 与 input_tokens 分列,
    合入 input_tokens 计量 —— SF pricing 页对两模型只标单一输入价
    (0.7 元/M, 2026-10-01 官方页截图实证), image 与文本同价;
    官方文档未单独定义 image token 单价, 同价合并为近似口径。
    """
    usage = data.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    input_tokens = usage.get("prompt_tokens") or 0
    output_tokens = usage.get("completion_tokens") or 0
    image_tokens = usage.get("image_tokens") or 0
    if not input_tokens:
        total = usage.get("total_tokens")
        if not isinstance(total, int):
            t = data.get("tokens")
            if isinstance(t, int):
                total = t
            elif isinstance(t, dict):
                total = t.get("input_tokens") or t.get("total_tokens")
                image_tokens = image_tokens or (t.get("image_tokens") or 0)
        # siliconflow rerank (Jina 兼容): 用量在 meta.tokens.input_tokens
        if not isinstance(total, int):
            meta = data.get("meta")
            if isinstance(meta, dict) and isinstance(meta.get("tokens"), dict):
                total = meta["tokens"].get("input_tokens") or meta["tokens"].get("total_tokens")
                image_tokens = image_tokens or (meta["tokens"].get("image_tokens") or 0)
                if not image_tokens:
                    billed = meta.get("billed_units")
                    if isinstance(billed, dict):
                        image_tokens = billed.get("image_tokens") or 0
        input_tokens = total or 0
    return int((input_tokens or 0) + (image_tokens or 0)), int(output_tokens or 0)


def _schedule_sf_usage_log(channel, upstream_name, resp, latency_ms,
                           caller_id, caller_key_id) -> None:
    """fire-and-forget 用量记录 (失败不影响主流程, 与 proxy 同款)"""
    try:
        try:
            data = resp.json()
        except Exception:
            data = {}
        input_tokens, output_tokens = _extract_tokens(data)
        cost_estimate = _estimate_cost(channel, upstream_name, input_tokens, output_tokens)
        list_cost_estimate = _estimate_list_cost(upstream_name, input_tokens, output_tokens)
        loop = asyncio.get_running_loop()
        task = loop.create_task(_write_usage_log(
            channel_id=channel.get("id"),
            account_id=channel.get("account_id"),
            model=upstream_name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            status_code=resp.status_code,
            latency_ms=latency_ms,
            caller_id=caller_id,
            caller_key_id=caller_key_id,
            cost_estimate=cost_estimate,
            list_cost_estimate=list_cost_estimate,
        ))
        task.add_done_callback(_log_task_result)
    except Exception:
        pass  # 用量记录失败不影响主流程


async def _execute(channel: dict, upstream_name: str, path: str, body_json: dict,
                   caller_id, caller_key_id):
    """转发到上游并原样返回响应"""
    channel = dict(channel)  # V4.9.7: 浅拷贝隔离 per-request 字段
    base_url = _resolve_effective_base_url(channel)
    target_url = f"{base_url.rstrip('/')}/{path}"
    if not _validate_upstream_url(target_url):
        return _error(f"Invalid upstream URL: {target_url}", 500, "api_error")

    upstream_body = dict(body_json)
    upstream_body["model"] = upstream_name

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {channel['api_key']}",
    }

    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(settings.request_timeout, connect=10.0)
        ) as client:
            resp = await client.post(target_url, headers=headers, json=upstream_body)
    except httpx.TimeoutException:
        return _error("Upstream timeout", 504, "timeout")
    except httpx.ConnectError:
        return _error("Cannot connect to upstream", 502, "api_error")
    except httpx.HTTPError as e:
        return _error(f"Upstream request failed: {e}", 502, "api_error")
    latency_ms = int((time.monotonic() - t0) * 1000)

    # V4.9 同款: 上游响应 → cooldown (单账号无 failover, 只记状态)
    from gateway.cooldown import cooldown_manager
    from gateway.error_classifier import classify
    if resp.status_code < 400:
        cooldown_manager.mark_success(channel["account_id"])
    elif resp.status_code in _RETRYABLE_STATUS:
        try:
            resp_body = resp.text
        except Exception:
            resp_body = None
        cooldown_manager.mark_failure(
            channel["account_id"], error_info=classify(resp.status_code, resp_body, None)
        )

    _schedule_sf_usage_log(channel, upstream_name, resp, latency_ms,
                           caller_id, caller_key_id)

    return Response(
        status_code=resp.status_code,
        content=resp.content,
        media_type=resp.headers.get("content-type", "application/json"),
    )


def _parse_body(request_body: bytes):
    """解析请求体, 返回 (body_json, error_response)"""
    try:
        body_json = json.loads(request_body)
    except json.JSONDecodeError:
        return None, _error("Invalid JSON body", 400)
    if not isinstance(body_json, dict):
        return None, _error("Invalid JSON body", 400)
    return body_json, None


@router.post("/v1/embeddings")
async def embeddings(request: Request):
    """OpenAI 兼容 embeddings 直通 (siliconflow)"""
    body_json, err = _parse_body(await request.body())
    if err is not None:
        return err
    model = body_json.get("model", "")
    if not model:
        return _error("model field is required", 400)
    if not body_json.get("input"):
        return _error("input field is required", 400)

    caller_id = getattr(request.state, "caller_id", None)
    caller_key_id = getattr(request.state, "caller_key_id", None)
    channel, upstream_name, err = await _prepare(model)
    if err is not None:
        return err
    return await _execute(channel, upstream_name, "embeddings", body_json,
                          caller_id, caller_key_id)


@router.post("/v1/rerank")
async def rerank(request: Request):
    """Jina 兼容 rerank 直通 (siliconflow)"""
    body_json, err = _parse_body(await request.body())
    if err is not None:
        return err
    model = body_json.get("model", "")
    if not model:
        return _error("model field is required", 400)
    documents = body_json.get("documents")
    if not body_json.get("query") or not isinstance(documents, list) or not documents:
        return _error("query and non-empty documents are required", 400)

    caller_id = getattr(request.state, "caller_id", None)
    caller_key_id = getattr(request.state, "caller_key_id", None)
    channel, upstream_name, err = await _prepare(model)
    if err is not None:
        return err
    return await _execute(channel, upstream_name, "rerank", body_json,
                          caller_id, caller_key_id)
