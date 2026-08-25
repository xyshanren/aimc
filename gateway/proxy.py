"""核心代理转发逻辑

关键原则：原样透传字节给客户端。
- 流式请求：使用 aiter_raw() 返回原始字节流
  P0-1 (2026-08-19): 透传的同时用滑动缓冲累积字节, 在流结束时
  扫描末尾 chunk 提取 usage (OpenAI 末尾 usage chunk / Anthropic message_delta),
  调用 _schedule_usage_log 记录, 不改变发给客户端的字节。
- 非流式请求：直接返回上游 JSON 响应
"""

# V4.6.4: 剥离请求里的 thinking 相关字段 (个人项目偏好 agent loop 更快)
# 默认上游 (minimax-m2.7) 即使不传 enable_thinking 也输出 thinking, 吃 token + 加延迟
# proxy 层在转发前主动剥离这些字段
THINKING_FIELDS_TO_STRIP = (
    "thinking",
    "enable_thinking",
    "reasoning_effort",
    "extended_thinking",
)

# P2-B (2026-08-20): drop_params 不会剥离这些字段 (无论 channel 怎么配置)
# messages = 请求内容, model = 路由必需, stream = 传输模式
ALWAYS_KEEP_PARAMS = frozenset({"messages", "model", "stream"})

# V4.8.7: 已知 OpenAI 请求参数白名单 — 只对这些参数做自动 drop 学习
# 避免从错误消息里误提取非参数字符串
_KNOWN_REQUEST_PARAMS = frozenset({
    "reasoning_effort", "max_tokens", "temperature", "top_p",
    "frequency_penalty", "presence_penalty", "logprobs", "top_logprobs",
    "n", "stop", "seed", "response_format", "tools", "tool_choice",
    "user", "metadata", "stream_options", "service_tier",
    "prompt_cache_key", "store", "logit_bias",
})

import json
import os
import time
import logging
import asyncio
from datetime import datetime
from typing import Optional

import httpx
from fastapi.responses import StreamingResponse, JSONResponse
from starlette.datastructures import Headers

from config import settings

logger = logging.getLogger(__name__)


def _estimate_cost(
    channel: dict,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens=0,
    cache_read_input_tokens=0,
    cache_creation_input_tokens=0,
) -> Optional[float]:
    """V4.8.5: 按当期价格估算单次请求费用（元）。

    价格单位 = 元/百万tokens, token 数需除以 1_000_000.
    三类 cache token 互斥 (同一请求只有一种风格):
    - cached_input_tokens (OpenAI cache_hit) → cached_input_price
    - cache_read_input_tokens (Anthropic cache_read) → cached_input_price
    - cache_creation_input_tokens (Anthropic cache_write) → cache_write_price
    非 cache 的 input 部分按 input_price 计费.

    返回 None 表示价格未找到 (e.g. 999 兜底价 / model 不在价格表).
    """
    try:
        from gateway.router import route_engine
        now = datetime.now()
        price = route_engine._get_current_price(channel, model, now)
        if price is None:
            price = route_engine._get_fuzzy_price(channel, model, now)
        if price is None:
            return None
    except Exception as e:
        logger.debug(f"cost_estimate 价格查询失败: model={model} err={e}")
        return None

    input_price = price.get("input_price") or 0.0
    output_price = price.get("output_price") or 0.0
    cached_price = price.get("cached_input_price")  # 可能为 None
    cache_write = price.get("cache_write_price")     # 可能为 None

    # cache 命中的 input 部分按 cached_price 计费 (若有), 否则按 input_price
    cache_hit_tokens = (cached_input_tokens or 0) + (cache_read_input_tokens or 0)
    normal_input_tokens = max(input_tokens - cache_hit_tokens, 0)

    cost = 0.0
    cost += normal_input_tokens * input_price
    cost += (output_tokens or 0) * output_price
    if cache_hit_tokens > 0 and cached_price is not None:
        cost += cache_hit_tokens * cached_price
    elif cache_hit_tokens > 0:
        # 无 cached_price 定价, fallback 按 input_price (保守)
        cost += cache_hit_tokens * input_price
    if (cache_creation_input_tokens or 0) > 0 and cache_write is not None:
        cost += (cache_creation_input_tokens or 0) * cache_write

    return cost / 1_000_000  # 元/百万tokens → 元


def _strip_thinking_fields(body_dict: dict) -> dict:
    """V4.6.4+: 剥离 body 字典里的 thinking 相关字段 (返回新 dict, 不修改原对象)

    Args:
        body_dict: 已解析的 request body (dict)

    Returns:
        新 dict (剥离了 thinking/enable_thinking/reasoning_effort/extended_thinking)

    Notes:
        - 只剥离顶层字段 (不递归 messages 内部, agent 可能把 reasoning 放 message 里)
        - immutable: 不修改输入 dict
    """
    if not any(k in body_dict for k in THINKING_FIELDS_TO_STRIP):
        return body_dict  # 没有 thinking 字段, 避免创建新 dict

    out = {k: v for k, v in body_dict.items() if k not in THINKING_FIELDS_TO_STRIP}
    return out


def _parse_drop_params(raw) -> list:
    """P2-B: 解析 channel.drop_params 为 list[str]

    支持两种输入:
    - JSON list string: '["reasoning_effort","max_tokens"]' → ["reasoning_effort", "max_tokens"]
    - 逗号分隔 string: "reasoning_effort,max_tokens" → ["reasoning_effort", "max_tokens"]
      (dashboard 输入框用逗号分隔更方便)
    """
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(k).strip() for k in raw if str(k).strip()]
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return []
        # 尝试 JSON 解析 (ORM 存的格式)
        if raw.startswith("["):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    return [str(k).strip() for k in parsed if str(k).strip()]
            except (json.JSONDecodeError, TypeError):
                pass
        # 逗号分隔 (dashboard 表单输入)
        return [k.strip() for k in raw.split(",") if k.strip()]
    return []


def _drop_params(body: bytes, channel: dict) -> tuple:
    """P2-B: 按 channel.drop_params 配置剥离不支持的上游参数 (deny-list)

    在 thinking strip 之后调用, 只过滤 body 顶层 key.
    ALWAYS_KEEP_PARAMS (messages/model/stream) 永远保留.

    Returns: (new_body, dropped_param_names)
    """
    raw = channel.get("drop_params")
    if not raw:
        return body, []
    drop_list = _parse_drop_params(raw)
    if not drop_list:
        return body, []

    try:
        body_dict = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body, []

    dropped = [k for k in drop_list if k in body_dict and k not in ALWAYS_KEEP_PARAMS]
    if not dropped:
        return body, []

    for k in dropped:
        del body_dict[k]
    new_body = json.dumps(body_dict, ensure_ascii=False).encode("utf-8")
    return new_body, dropped


def _extract_unsupported_params(response, body: bytes) -> list[str]:
    """V4.8.7: 从上游 400 响应中提取被拒绝的参数名

    覆盖主流上游错误格式:
    1. OpenAI 风格: {"error": {"param": "reasoning_effort", ...}}
    2. 消息文本提取: "unsupported parameter: 'reasoning_effort'"
       / "unknown parameter 'reasoning_effort'"
       / "unrecognized request argument supplied: 'reasoning_effort'"

    安全过滤:
    - 只保留白名单 _KNOWN_REQUEST_PARAMS 中的参数名
    - 只保留 body 顶层已存在的 key (避免误杀)
    - ALWAYS_KEEP_PARAMS 永远不提取
    """
    try:
        data = response.json()
    except Exception:
        return []
    err = data.get("error", data) if isinstance(data, dict) else {}
    if not isinstance(err, dict):
        return []

    candidates: list[str] = []
    # 1. OpenAI 风格 error.param
    param = err.get("param")
    if isinstance(param, str) and param:
        candidates.append(param)

    # 2. 从 message 文本提取引号包裹的参数名
    msg = err.get("message", "")
    if isinstance(msg, str):
        import re
        for m in re.finditer(r"['\"]([a-z_][a-z0-9_]*)['\"]", msg):
            candidates.append(m.group(1))

    # 过滤: 白名单 + body 存在 + 非 ALWAYS_KEEP_PARAMS
    try:
        body_dict = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        body_dict = {}

    result: list[str] = []
    seen: set[str] = set()
    for p in candidates:
        p = p.strip().lower()
        if p in seen:
            continue
        if p in ALWAYS_KEEP_PARAMS:
            continue
        if p not in _KNOWN_REQUEST_PARAMS:
            continue
        if p not in body_dict:
            continue
        seen.add(p)
        result.append(p)
    return result


async def _auto_learn_drop_params(channel: dict, params: list[str]):
    """V4.8.7: 异步把新参数追加到 channel.drop_params, 刷新 router 缓存

    fire-and-forget: 不阻塞响应, 失败只 log.
    """
    try:
        from db.engine import async_session_factory
        from db.models import Channel
        from sqlalchemy import select
        from gateway.router import route_engine

        async with async_session_factory() as session:
            ch = (await session.execute(
                select(Channel).where(Channel.id == channel["id"])
            )).scalars().first()
            if not ch:
                return
            existing = _parse_drop_params(ch.drop_params)
            new_params = [p for p in params if p not in existing]
            if not new_params:
                return
            merged = existing + new_params
            ch.drop_params = json.dumps(merged, ensure_ascii=False)
            await session.commit()
            logger.warning(
                f"V4.8.7 auto-drop: channel={channel['name']} "
                f"learned {new_params} (drop_params={merged})"
            )
        try:
            await route_engine.invalidate_channel(channel["id"])
        except Exception:
            pass
    except Exception as e:
        logger.debug(f"V4.8.7 auto-learn failed (non-fatal): {e}")


def _maybe_rewrite_body_model(
    body: bytes,
    channel: dict,
    requested_model: str = "",
    actual_model: Optional[str] = None,
) -> bytes:
    """V4.6.4 Issue 6: 改写 body.model 为 channel 实际模型名

    fallback 路径 router 选了 channel 但 model 名跟用户请求的不同。
    例: 用户请求 glm-5.2, channel 只有 zai-org/GLM-5.2。
    替换 body.model = channel 支持的 model, 上游能识别。

    V4.6.5: caller 传入 _channel_model (fuzzy 匹配的 price model),
    优先用它替换 body. 若 caller 未传 (None), fallback 到 _actual_model /
    requested_model.

    Args:
        body: 原始请求体字节
        channel: 渠道信息字典
        requested_model: 用户请求的 model 字符串
        actual_model: 显式传入的目标 model (优先于 channel["_actual_model"])

    Returns:
        改写后的 body 字节 (未改时原样返回)
    """
    actual_model = actual_model or channel.get("_channel_model") or channel.get("_actual_model") or requested_model
    if actual_model == requested_model:
        return body  # 无需改写

    try:
        body_json = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.warning(f"body 解析失败, 跳过 model 替换: {e}")
        return body

    if body_json.get("model") == actual_model:
        return body  # 已是目标值, 不重复改

    old_model = body_json.get("model")
    body_json["model"] = actual_model
    logger.info(
        f"[V4.6.5 body model 替换] {old_model} -> {actual_model} "
        f"(channel={channel.get('name')})"
    )
    return json.dumps(body_json, ensure_ascii=False).encode("utf-8")


def _resolve_effective_base_url(channel):
    # P1-12: 解析 channel 实际使用的 base_url
    # 优先级: db.api_base ?? SENSENOVA_BASE_URL env (sensenova only) ?? db.base_url
    api_base = (channel.get("api_base") or "").strip()
    if api_base:
        return api_base.rstrip("/")
    db_base_url = (channel.get("base_url") or "").rstrip("/")
    if channel.get("provider") == "sensenova":
        env_override = os.getenv("SENSENOVA_BASE_URL", "").strip().rstrip("/")
        if env_override and env_override != db_base_url:
            return env_override
    return db_base_url


def _stable_prefix_cache_key(messages: list, seg_limit: int = 4096) -> str:
    """P3 (2026-08-21): 从 messages 提取稳定前缀的分段 hash cache key

    策略: sha256(全量 system 拼接 + "\\x1f" + 第一条非 system 消息)[:16]
    - 多轮 agent loop: system + 首条 user 跨轮不变 → 同 key → 上游同 cache shard → 前缀命中
    - 无 system 时 fallback 用首条 user 消息 (消灭全局空串 key)
    - 每段截断 seg_limit 字符, 避免超大 payload hash 开销
    """
    import hashlib

    def _msg_text(msg) -> str:
        """取 message 的文本内容 (str 或 content parts list)"""
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                p.get("text", "") for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            )
        return ""

    system_parts = []
    first_non_system = None
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "system":
            system_parts.append(_msg_text(msg))
        elif first_non_system is None:
            first_non_system = _msg_text(msg)

    seg1 = "\n".join(system_parts)[:seg_limit]
    seg2 = (first_non_system or "")[:seg_limit]
    return hashlib.sha256(f"{seg1}\x1f{seg2}".encode("utf-8")).hexdigest()[:16]


def _inject_prompt_cache(body, channel):
    # P1-8: 给 request body 注入 prompt cache 标记
    # P3 (2026-08-21) 路径 A 优化: 分段 hash 稳定前缀 (system 全量 + 首条非 system),
    # 保留调用方已传的 prompt_cache_key, Anthropic 分支加 role 校验
    # 支持 2 种 cache 协议 (按 channel.api_style):
    # 1. Anthropic 风格: system 消息 + 第一条 user 消息加 cache_control
    # 2. OpenAI 风格: 加 prompt_cache_key parameter (稳定前缀分段 hash)
    # 触发条件 (caller 保证): channel.supports_prompt_cache=1
    try:
        body_json = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body

    messages = body_json.get("messages")
    if not isinstance(messages, list) or not messages:
        return body

    api_style = (channel.get("api_style") or "openai").lower()

    if api_style == "anthropic":
        # Anthropic 风格: 给 system 消息 + 第一条 user 消息加 cache_control: ephemeral
        # (P3: 校验 role — 之前盲目标 messages[:2], assistant/tool 也可能被标)
        marked_user = False
        for msg in messages:
            if not isinstance(msg, dict) or msg.get("cache_control"):
                continue
            role = msg.get("role")
            if role == "system":
                msg["cache_control"] = {"type": "ephemeral"}
            elif role == "user" and not marked_user:
                msg["cache_control"] = {"type": "ephemeral"}
                marked_user = True
    else:
        # OpenAI 风格: prompt_cache_key parameter
        # P3: 调用方已传 key 时不覆盖 (跟 Anthropic 分支保留 cache_control 对称)
        if body_json.get("prompt_cache_key"):
            return body
        body_json["prompt_cache_key"] = _stable_prefix_cache_key(messages)

    return json.dumps(body_json, ensure_ascii=False).encode("utf-8")


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
    # V4.6.5: 区分三个字段
    # - _registry_model: registry model_id (响应头用, e.g. glm-4.5v)
    # - _channel_model: fuzzy 匹配的 price model (body 替换用, e.g. zai-org/GLM-4.5V)
    # - _actual_model: V4.6.4 兼容, 精确路由时用 requested_model
    is_group_request = requested_model.startswith(("tier:", "scene:"))
    if is_group_request and channel.get("_registry_model"):
        actual_model = channel["_registry_model"]
    elif is_group_request and channel.get("_actual_model"):
        # 组路由 (V4.6.5 前): 用 _actual_model (Phase 2.4 注入)
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


async def forward_request(
    channel: dict, body: bytes, headers: Headers,
    requested_model: str = "", actual_model: Optional[str] = None,
    caller_id: Optional[int] = None,
    caller_key_id: Optional[int] = None,
):
    """将请求透传到上游渠道

    Args:
        channel: 渠道信息字典，包含 base_url, api_key 等
        body: 原始请求体字节
        headers: 原始请求头
        requested_model: 请求的 model 字符串 (用户原始请求, 用于响应头 X-AIMC-Actual-Model)
        actual_model: V4.6.4+ — router fallback 路径选择的 channel 模型名
                      (可能跟 requested_model 不同, 此时会替换 body.model)
        caller_id: V4.8 — 调用者 user_id (从 request.state 透传), 写入 usage_logs.caller_id
        caller_key_id: V4.8.1 — api_keys.id (从 request.state 透传), 写入 usage_logs.key_id

    Returns:
        StreamingResponse 或 JSONResponse
    """
    # V4.6.4: fallback 路径 router 选了 channel 但 model 名跟用户请求的不同
    # 例: 用户请求 deepseek-v2.5, channel 只有 deepseek-ai/DeepSeek-V3
    # 替换 body.model = channel 支持的 model, 上游能识别
    actual_model = actual_model or requested_model

    # V4.6.4: thinking 剥离 (默认启用, channel.disable_thinking_pass_through=1)
    # 某些上游 (minimax deepthink) 即使不传 enable_thinking 也强制输出 thinking.
    # 剥离 request 里的 thinking/enable_thinking/reasoning_effort/extended_thinking 字段
    # → 节省 token + agent loop 更快
    if channel.get("disable_thinking_pass_through", 1):
        try:
            import json as _tjson
            _body_dict = _tjson.loads(body)
            _stripped_body = _strip_thinking_fields(_body_dict)
            if _stripped_body is not _body_dict:  # 有字段被剥离
                _stripped = [k for k in THINKING_FIELDS_TO_STRIP if k in _body_dict]
                body = _tjson.dumps(_stripped_body, ensure_ascii=False).encode("utf-8")
                logger.info(f"[V4.6.4 thinking 剥离] channel={channel.get('name')} stripped={_stripped}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass  # 非 JSON body, 跳过

    # P2-B (2026-08-20): 按 channel.drop_params 剥离不支持的上游参数
    # 在 thinking strip 之后, model rewrite 之前, 只过滤 body 顶层 key
    new_body, dropped = _drop_params(body, channel)
    if dropped:
        body = new_body
        logger.info(f"[P2-B drop_params] channel={channel.get('name')} dropped={dropped}")

    # V4.6.5: body 替换用 _channel_model (router fuzzy 匹配的 price model, 上游认识)
    # _channel_model 由 router._select_by_group / router.select_best_channel 写入
    # (e.g. zai-org/GLM-4.5V for glm-4.5v). 跟 _actual_model (响应头用 registry model) 分离.
    channel_model = channel.get("_channel_model")
    if channel_model and channel_model != requested_model:
        body = _maybe_rewrite_body_model(body, channel, requested_model, channel_model)
    elif actual_model != requested_model:
        body = _maybe_rewrite_body_model(body, channel, requested_model, actual_model)

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

    # P1-8 (2026-08-14): prompt cache headers 注入
    # 只在 channel.supports_prompt_cache=1 时启用
    # Anthropic 风格: messages[0] (system) 加 cache_control: {type: "ephemeral"}
    # OpenAI 风格: 加 prompt_cache_key parameter (用 system content hash)
    # V4.9.3: settings.prompt_cache_inject_enabled 全局开关 (A/B 对比用)
    if channel.get("supports_prompt_cache") and settings.prompt_cache_inject_enabled:
        try:
            body = _inject_prompt_cache(body, channel)
        except Exception as e:
            logger.warning(f"[P1-8] prompt cache 注入失败 (非致命): {e}")

    base_url = _resolve_effective_base_url(channel)
    target_url = f"{base_url.rstrip('/')}/chat/completions"

    start_time = time.time()

    try:
        if is_stream:
            return await _forward_stream(
                target_url, forward_headers, body, channel, start_time, aimc_headers,
                requested_model, caller_id, caller_key_id,
            )
        else:
            return await _forward_normal(
                target_url, forward_headers, body, channel, start_time, aimc_headers,
                requested_model, caller_id, caller_key_id,
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
    caller_id: Optional[int] = None,
    caller_key_id: Optional[int] = None,
) -> StreamingResponse:
    """流式透传：原样传递字节给客户端

    P0-1 (2026-08-19): 透传的同时用滑动缓冲累积字节, 在流结束时
    扫描末尾 SSE chunk 提取 usage, 调用 _schedule_stream_usage_log 记录。
    不解析/改变发给客户端的字节, usage 提取只在 finally 里离线扫描缓冲。

    V4.8: caller_id 透传到 _schedule_stream_usage_log -> _write_usage_log
    V4.8.1: caller_key_id 透传到 _schedule_stream_usage_log -> _write_usage_log
    """

    client = httpx.AsyncClient(timeout=httpx.Timeout(settings.request_timeout, connect=10.0))

    req = client.build_request(
        method="POST",
        url=url,
        headers=headers,
        content=body,
    )

    response = await client.send(req, stream=True)

    # V4.9.1 修复: stream 模式下 response.text 会失败 (body 还在 stream),
    # 必须先 await response.aread() 缓冲到内存, 才能用 .text 提取 error code/message.
    # 不然 classify(429, None, None) 走 status_code fallback 误判为 rate_limited,
    # 错过 quota_exhausted 关键字, needs_human 永远不触发.
    if response.status_code >= 400:
        try:
            await response.aread()
        except Exception:
            pass

    # V4.9: 上游响应 → error_classifier 统一处理 cooldown + needs_human
    _update_cooldown(channel, response)

    # Phase 2E: 上游错误 (5xx + 401/403) 触发告警
    if response.status_code in (401, 403, 500, 502, 503, 504):
        _schedule_alert(channel, response)

    # V4.8.7: 流式 400 同样自动学习 drop_params
    if response.status_code == 400 and channel.get("auto_drop_params", 1):
        bad_params = _extract_unsupported_params(response, body)
        if bad_params:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(_auto_learn_drop_params(channel, bad_params))
            except RuntimeError:
                pass

    async def stream_generator():
        # P0-1: 滑动缓冲, 只保留末尾 N 字节用于扫描 usage chunk
        # usage 出现在流的最后 (OpenAI: [DONE] 前一个 chunk; Anthropic: message_delta 事件)
        # 保留 64KB 足以覆盖绝大多数 usage chunk, 避免大流量时内存膨胀
        tail_buffer = bytearray()
        TAIL_LIMIT = 65536
        # V4.9.3: 转录用全量缓冲 (不做滑动窗口, 但受 transcript_log_max_body 截断)
        full_buffer = bytearray() if settings.transcript_log_enabled else None
        try:
            async for chunk in response.aiter_raw():
                # 透传给客户端 (原样, 不修改)
                yield chunk
                # 累积到尾部缓冲用于事后提取 usage
                tail_buffer.extend(chunk)
                if len(tail_buffer) > TAIL_LIMIT:
                    # 只保留末尾 TAIL_LIMIT 字节 (滑动窗口)
                    del tail_buffer[: len(tail_buffer) - TAIL_LIMIT]
                # V4.9.3: 转录全量累积 (截断上限内)
                if full_buffer is not None:
                    full_buffer.extend(chunk)
        finally:
            await response.aclose()
            await client.aclose()
            latency_ms = int((time.time() - start_time) * 1000)
            logger.info(
                f"[Stream] {channel['name']} -> {response.status_code} ({latency_ms}ms)"
            )
            # P0-1: 从尾部缓冲提取 usage 并记录 (不阻塞, fire-and-forget)
            try:
                _schedule_stream_usage_log(
                    channel, response.status_code, latency_ms,
                    requested_model, bytes(tail_buffer), caller_id, caller_key_id,
                )
            except Exception as e:
                logger.debug(f"[P0-1] stream usage 记录失败 (非致命): {e}")
            # V4.9.3: 请求/响应转录 (诊断用, 默认关)
            if full_buffer is not None:
                try:
                    _schedule_transcript_log(
                        channel, body, None, requested_model,
                        caller_id, caller_key_id,
                        stream_status=response.status_code,
                        stream_body=bytes(full_buffer),
                    )
                except Exception as e:
                    logger.debug(f"[V4.9.3] stream 转录记录失败 (非致命): {e}")

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
    caller_id: Optional[int] = None,
    caller_key_id: Optional[int] = None,
) -> JSONResponse:
    """非流式转发：等待完整响应后返回

    V4.6.1: 接收 requested_model 透传给 _schedule_usage_log
    V4.8: 接收 caller_id 透传给 _schedule_usage_log
    V4.8.1: 接收 caller_key_id 透传给 _schedule_usage_log
    """

    async with httpx.AsyncClient(timeout=httpx.Timeout(settings.request_timeout, connect=10.0)) as client:
        response = await client.post(url, headers=headers, content=body)

    latency_ms = int((time.time() - start_time) * 1000)
    logger.info(f"[Normal] {channel['name']} -> {response.status_code} ({latency_ms}ms)")

    # V4.9: 上游响应 → error_classifier 统一处理 cooldown + needs_human
    _update_cooldown(channel, response)

    # Phase 2E: 上游错误 (5xx + 401/403) 触发告警
    if response.status_code in (401, 403, 500, 502, 503, 504):
        _schedule_alert(channel, response)

    # V4.8.7: 上游 400 时自动学习 drop_params
    if response.status_code == 400 and channel.get("auto_drop_params", 1):
        bad_params = _extract_unsupported_params(response, body)
        if bad_params:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(_auto_learn_drop_params(channel, bad_params))
            except RuntimeError:
                pass

    # 尝试记录用量（异步，不阻塞响应）
    _schedule_usage_log(channel, response, latency_ms, requested_model, caller_id, caller_key_id)

    # V4.9.3: 请求/响应转录 (诊断用, 默认关)
    if settings.transcript_log_enabled:
        _schedule_transcript_log(
            channel, body, response, requested_model,
            caller_id, caller_key_id,
        )

    return JSONResponse(
        status_code=response.status_code,
        content=response.json() if response.headers.get("content-type", "").startswith("application/json") else {"raw": response.text},
        headers=aimc_headers or {},
    )


def _extract_group_id(requested_model: str) -> str | None:
    """Phase 3.A: 从 requested_model 提取 group_id

    - tier:xxx / scene:xxx 前缀 → 返回 group_id (e.g. "tier:balanced")
    - 精确模型名 → 返回 None (不需要记录 group)
    - V4.6.5 fix: 剥离 /model 后缀, 跟 router.py select_channel 对齐
      (e.g. "tier:vision/glm-4.5v" → "tier:vision", 否则 dashboard 显示两行)
    """
    if requested_model and requested_model.startswith(("tier:", "scene:")):
        return requested_model.split("/")[0]
    return None


def _update_cooldown(channel: dict, status_code_or_response) -> None:
    """V4.6.1 → V4.9: 路由转发后更新 cooldown_manager.

    V4.9 重构:
    - 接收 httpx.Response 对象 (或仅 status_code 兼容旧调用)
    - 用 error_classifier.classify 决定 cooldown 行为
    - 200 → mark_success
    - needs_human=True (quota/auth) → mark_failure + needs_human=True
    - network_transient/5xx/rate_limited → mark_failure (按 suggested_cooldown_seconds)
    - client_error → 不动 (不进 cooldown)

    channel 无 account_id 时静默跳过.
    """
    account_id = channel.get("account_id")
    if not account_id:
        return
    try:
        import httpx as _httpx
        from gateway.cooldown import cooldown_manager
        from gateway.error_classifier import ErrorCategory, classify

        # 向后兼容: 只传 status_code (int) 时走老逻辑
        if isinstance(status_code_or_response, int):
            status_code = status_code_or_response
            response_body = None
        else:
            # httpx.Response
            status_code = status_code_or_response.status_code
            try:
                response_body = status_code_or_response.text
            except Exception:
                response_body = None

        if status_code == 200:
            cooldown_manager.mark_success(account_id)
            return

        info = classify(status_code, response_body, None)

        # client_error 不进 cooldown (跟旧规则一致)
        if info.category == ErrorCategory.CLIENT_ERROR:
            return

        # 走 mark_failure(error_info=...) 新签名 (由 Commit 2 实现)
        cooldown_manager.mark_failure(account_id, error_info=info)

        # V4.9: needs_human=True 的错误触发专门告警 (quota/auth/model_unsupported)
        # 之前的 _schedule_alert 只覆盖 5xx+401/403, 漏了 420/余额不足等
        if info.needs_human:
            _schedule_alert_with_info(channel, info)
    except Exception as e:
        logger.debug(f"_update_cooldown 非致命: {e}")


def _resolve_actual_model(channel: dict, requested_model: str, fallback_model: str = "") -> str:
    """统一解析实际命中的 model 名 (P0-3 fix + V4.6.5)

    背景: router 注入的 _actual_model / _registry_model 写在共享 channel dict 上
    (非 copy), 组路由命中后该字段会残留, 后续精确路由命中同一 channel 时
    _actual_model 仍是上一次组路由的值, 导致 usage_logs.model 记错.

    规则 (与 _build_aimc_headers 保持一致):
    - 组路由 (requested_model 以 tier:/scene: 开头) 且 channel 有 _registry_model
      -> 用 _registry_model (V4.6.5: registry model_id, e.g. glm-4.5v)
    - 组路由 且 channel 有 _actual_model (V4.6.5 前兼容)
      -> 用 _actual_model
    - 其他 -> 用 requested_model, 再 fallback 到 fallback_model (上游返回的 model)

    注意: 用 _registry_model 而非 _channel_model — usage_logs 记的是
    registry 侧 model_id (e.g. "glm-4.5v"), 不是上游 price model 名
    (e.g. "zai-org/GLM-4.5V"), 跟 model_registry 表对齐, 方便统计.
    """
    is_group_request = bool(requested_model) and requested_model.startswith(("tier:", "scene:"))
    if is_group_request:
        # V4.6.5: 优先 _registry_model, fallback _actual_model (V4.6.5 前路径)
        if channel.get("_registry_model"):
            return channel["_registry_model"]
        if channel.get("_actual_model"):
            return channel["_actual_model"]
    # P2-A: 精确路由 fallback 链 — _channel_model (fuzzy match 后的上游名)
    # 优先于 requested_model, 避免上游 400 错误响应无 model 字段时 usage_logs.model 为空
    return channel.get("_channel_model") or requested_model or fallback_model or ""


def _parse_cache_tokens(usage: dict):
    """从 usage dict 解析 cache token (OpenAI + Anthropic 风格)

    Returns: (cached_input_tokens, cache_read_input_tokens, cache_creation_input_tokens)
    每个 None = 上游未返回该字段
    """
    cached_input_tokens = None
    cache_read_input_tokens = None
    cache_creation_input_tokens = None

    # OpenAI: usage.prompt_tokens_details.cached_tokens
    prompt_details = usage.get("prompt_tokens_details")
    if isinstance(prompt_details, dict):
        v = prompt_details.get("cached_tokens")
        if isinstance(v, int):
            cached_input_tokens = v

    # Anthropic: usage.cache_read_input_tokens + cache_creation_input_tokens
    v = usage.get("cache_read_input_tokens")
    if isinstance(v, int):
        cache_read_input_tokens = v
    v = usage.get("cache_creation_input_tokens")
    if isinstance(v, int):
        cache_creation_input_tokens = v

    return cached_input_tokens, cache_read_input_tokens, cache_creation_input_tokens


def _schedule_usage_log(channel: dict, response: httpx.Response, latency_ms: int, requested_model: str = "", caller_id: Optional[int] = None, caller_key_id: Optional[int] = None):
    """异步记录用量日志（fire-and-forget）

    Phase 3.A: 接收 requested_model, 提取 group_id 写入 usage_logs
    - 精确模型路由: group_id = NULL
    - 组路由: group_id = "tier:xxx" / "scene:xxx"

    P0-3 fix (2026-08-19): model 名解析改用 _resolve_actual_model
    - 加 is_group_request 守卫, 避免共享 channel dict 的 _actual_model 残留污染
    - fallback 到 requested_model, 再 fallback 到上游响应 data.model

    V4.8: caller_id 透传到 _write_usage_log (多用户计量)
    V4.8.1: caller_key_id 透传到 _write_usage_log (多 key label 下钻)
    """
    try:
        data = response.json()
        usage = data.get("usage", {})
        group_id = _extract_group_id(requested_model)
        actual_model = _resolve_actual_model(
            channel, requested_model, fallback_model=data.get("model", "")
        )

        # P0-3 follow-up: 解析 cached_tokens (OpenAI + Anthropic 风格)
        cached_input_tokens, cache_read_input_tokens, cache_creation_input_tokens = (
            _parse_cache_tokens(usage)
        )

        # V4.8.5: 估算单次请求费用 (激活成本告警 / 周报 / 看板省钱量化)
        cost_estimate = _estimate_cost(
            channel, actual_model,
            usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0),
            cached_input_tokens, cache_read_input_tokens, cache_creation_input_tokens,
        )

        # 创建异步任务写入数据库
        # 修复 V4.6.4: 用 asyncio.create_task 替代 asyncio.get_event_loop().create_task
        # 之前: get_event_loop() 在 Python 3.12 + uvicorn 多 worker 场景下可能返回错的 loop 或 RuntimeError
        # 现在: 直接用 asyncio.create_task (Python 3.10+ 推荐)
        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(
                _write_usage_log(
                    channel_id=channel.get("id"),
                    account_id=channel.get("account_id"),
                    model=actual_model,
                    input_tokens=usage.get("prompt_tokens", 0),
                    output_tokens=usage.get("completion_tokens", 0),
                    status_code=response.status_code,
                    latency_ms=latency_ms,
                    group_id=group_id,
                    cached_input_tokens=cached_input_tokens,
                    cache_read_input_tokens=cache_read_input_tokens,
                    cache_creation_input_tokens=cache_creation_input_tokens,
                    caller_id=caller_id,
                    caller_key_id=caller_key_id,
                    cost_estimate=cost_estimate,
                )
            )
            # V4.6.4: 添加回调, task 失败时 log 错误 (之前静默)
            task.add_done_callback(_log_task_result)
        except RuntimeError as e:
            # 没有 running loop (不可能在 FastAPI handler 里, 但兜底)
            logger.warning(f"V4.6.4: asyncio.create_task 失败: {e}, 跳过本次 usage_log")
    except Exception:
        pass  # 用量记录失败不影响主流程


def _log_task_result(task: asyncio.Task) -> None:
    """V4.6.4: create_task 失败回调 — 让 _write_usage_log 异常可见"""
    try:
        exc = task.exception()
        if exc:
            logger.warning(f"用量日志写入失败 (task 异常): {exc}")
    except asyncio.CancelledError:
        pass


async def _write_usage_log(
    channel_id: int | None,
    account_id: int | None,
    model: str,
    input_tokens: int,
    output_tokens: int,
    status_code: int,
    latency_ms: int,
    group_id: str | None = None,
    cached_input_tokens=None,
    cache_read_input_tokens=None,
    cache_creation_input_tokens=None,
    caller_id: int | None = None,
    caller_key_id: int | None = None,
    cost_estimate: Optional[float] = None,
):
    """写入用量日志到数据库

    Phase 3.A: group_id 参数
    - 精确模型路由时为 None
    - 组路由时为 "tier:xxx" / "scene:xxx"
    - 供 Dashboard 命中率聚合 GROUP BY group_id, model

    V4.8: caller_id 参数 (多用户计量)
    - NULL = legacy 数据 / 未鉴权请求
    - 非 NULL = users.id (middleware 透传)

    V4.8.1: caller_key_id 参数 (多 key label 下钻)
    - NULL = legacy / 未鉴权 / 单 key 模式
    - 非 NULL = api_keys.id (middleware 透传)

    V4.8.5: cost_estimate 参数 (单次请求估算费用, 元)
    - NULL = 价格未找到 / 999 兜底价场景
    - 非 NULL = 按 _estimate_cost 算出的费用, 激活 v44_alerts 周环比告警 + 看板省钱量化
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
                caller_id=caller_id,
                key_id=caller_key_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                status_code=status_code,
                latency_ms=latency_ms,
                # P0-3 follow-up: 3 个 cache token 字段
                cached_input_tokens=cached_input_tokens,
                cache_read_input_tokens=cache_read_input_tokens,
                cache_creation_input_tokens=cache_creation_input_tokens,
                # V4.8.5: 估算费用 (激活成本告警 / 周报 / 看板省钱量化)
                cost_estimate=cost_estimate,
            )
            session.add(log)
            await session.commit()
    except Exception as e:
        logger.warning(f"用量日志写入失败: {e}")


def _schedule_transcript_log(
    channel, request_body, response, requested_model="",
    caller_id=None, caller_key_id=None,
    stream_status=None, stream_body=None,
):
    """V4.9.3: fire-and-forget 写 request_transcripts 表

    记录发给上游的 request body (注入后) + 上游响应 body,
    用于排查 "缓存注入是否导致降智" — 对比开/关 cache 的输出质量。
    失败不影响主流程 (pass)。
    """
    try:
        max_len = settings.transcript_log_max_body
        # 请求 body (注入后的, 即真正发给上游的)
        req_text = request_body.decode("utf-8", errors="replace")[:max_len]
        # 响应 body
        if stream_body is not None:
            resp_text = stream_body.decode("utf-8", errors="replace")[:max_len]
            status_code = stream_status
        elif response is not None:
            resp_text = response.text[:max_len] if response.text else ""
            status_code = response.status_code
        else:
            resp_text = ""
            status_code = stream_status or 0

        loop = asyncio.get_running_loop()
        task = loop.create_task(_write_transcript_log(
            channel_id=channel.get("id"),
            model=requested_model,
            request_body=req_text,
            response_body=resp_text,
            status_code=status_code,
            caller_id=caller_id,
            caller_key_id=caller_key_id,
        ))
        task.add_done_callback(_log_task_result)
    except Exception:
        pass  # 转录失败不影响主流程


async def _write_transcript_log(
    channel_id, model, request_body, response_body,
    status_code, caller_id=None, caller_key_id=None,
):
    """V4.9.3: 异步写入 request_transcripts 表"""
    try:
        from db.engine import async_session_factory
        from db.models import RequestTranscript
        async with async_session_factory() as session:
            log = RequestTranscript(
                channel_id=channel_id,
                model=model,
                request_body=request_body,
                response_body=response_body,
                status_code=status_code,
                caller_id=caller_id,
                key_id=caller_key_id,
            )
            session.add(log)
            await session.commit()
    except Exception as e:
        logger.warning(f"转录日志写入失败: {e}")


def _extract_stream_usage(tail_bytes: bytes) -> dict | None:
    """P0-1: 从 SSE 流的尾部字节提取 usage 对象

    OpenAI 风格: 流末尾在 [DONE] 前发一个 data: {"usage": {...}, ...} chunk
    Anthropic 风格: message_delta 事件的 usage 字段
    兼容两种, 扫描所有 `data: ` 行, 取最后一个含 usage 的 JSON。

    Returns: usage dict 或 None (没找到 / 解析失败)
    """
    try:
        text = tail_bytes.decode("utf-8", errors="ignore")
    except Exception:
        return None

    last_usage = None
    last_model = None
    # 逐行扫描 `data: {...}` 行
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue
        # 记下最后一个出现的 model (OpenAI 把 model 放在每个 chunk)
        if obj.get("model"):
            last_model = obj["model"]
        # OpenAI: 顶层 usage; Anthropic: message_delta.usage
        usage = obj.get("usage")
        if isinstance(usage, dict):
            last_usage = usage
    if last_usage is None:
        return None
    # 附带 model (若 usage 里没有, 用扫描到的最后一个 model)
    if last_model and "model" not in last_usage:
        last_usage["_stream_model"] = last_model
    return last_usage


def _schedule_stream_usage_log(
    channel: dict, status_code: int, latency_ms: int,
    requested_model: str, tail_bytes: bytes,
    caller_id: Optional[int] = None,
    caller_key_id: Optional[int] = None,
):
    """P0-1: 从 streaming 尾部缓冲提取 usage 并记录 (fire-and-forget)

    与 _schedule_usage_log 对应, 但输入是原始 SSE 尾部字节而非完整 response。
    V4.8: caller_id 透传到 _write_usage_log
    V4.8.1: caller_key_id 透传到 _write_usage_log
    """
    try:
        usage = _extract_stream_usage(tail_bytes)
        if usage is None:
            # 上游未返回 usage (部分 provider stream 不带 usage), 跳过
            logger.debug(
                f"[P0-1] stream 未提取到 usage, channel={channel.get('name')}, "
                f"requested={requested_model}"
            )
            return

        group_id = _extract_group_id(requested_model)
        # 流式没有完整 response.json(), fallback_model 用扫描到的 _stream_model
        fallback_model = usage.pop("_stream_model", "") or ""
        actual_model = _resolve_actual_model(
            channel, requested_model, fallback_model=fallback_model
        )

        cached_input_tokens, cache_read_input_tokens, cache_creation_input_tokens = (
            _parse_cache_tokens(usage)
        )

        # OpenAI: prompt_tokens / completion_tokens
        # Anthropic (message_delta): input_tokens / output_tokens (注: 流式可能只有 output_tokens)
        input_tokens = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
        output_tokens = usage.get("completion_tokens") or usage.get("output_tokens") or 0

        # V4.8.5: 估算单次请求费用 (激活成本告警 / 周报 / 看板省钱量化)
        cost_estimate = _estimate_cost(
            channel, actual_model,
            input_tokens, output_tokens,
            cached_input_tokens, cache_read_input_tokens, cache_creation_input_tokens,
        )

        # P0-1: 对齐 V4.6.4 修复 — 用 get_running_loop() 替代 get_event_loop()
        # get_event_loop() 在 Python 3.12 + uvicorn 多 worker 下可能返回错的 loop 或 RuntimeError
        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(
                _write_usage_log(
                    channel_id=channel.get("id"),
                    account_id=channel.get("account_id"),
                    model=actual_model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    status_code=status_code,
                    latency_ms=latency_ms,
                    group_id=group_id,
                    cached_input_tokens=cached_input_tokens,
                    cache_read_input_tokens=cache_read_input_tokens,
                    cache_creation_input_tokens=cache_creation_input_tokens,
                    caller_id=caller_id,
                    caller_key_id=caller_key_id,
                    cost_estimate=cost_estimate,
                )
            )
            task.add_done_callback(_log_task_result)
        except RuntimeError as e:
            logger.warning(f"[P0-1] stream asyncio.create_task 失败: {e}, 跳过本次 usage_log")
    except Exception:
        pass  # 用量记录失败不影响主流程


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
        loop = asyncio.get_running_loop()
        loop.create_task(
            _send_alert_async(msg, key=throttle_key)
        )
    except RuntimeError:
        # 没 event loop (e.g. 关闭时), 同步发
        logger.warning(f"[alerter fallback] {msg}")
    except Exception as e:
        # V4.6.4: 静默错误 — 之前 _schedule_alert 失败完全没 log
        logger.warning(f"V4.6.4: _schedule_alert task 调度失败: {e}")


async def _schedule_alert_with_info(channel: dict, error_info) -> None:
    """V4.9: needs_human=True 的错误触发告警 (quota/auth/model_unsupported)

    跟 _schedule_alert 区别:
    - throttle key 含 error_type, 同 channel + 同 error_type 1 小时静默
    - message 带错误详情 (code + msg + needs_human 提示)
    - 场景: 余额不足 / key 失效 / 模型不支持, 都需要人工介入
    """
    from gateway.error_classifier import ErrorInfo
    channel_name = channel.get("name", "?")
    msg = (
        f"[AIMC 需人工干预] {channel_name} {error_info.category.value}\n"
        f"账号: {channel.get('account_name', '?')}\n"
        f"错误码: {error_info.error_code or '-'}\n"
        f"错误: {error_info.error_msg[:200] or '-'}\n"
        f"操作: dashboard '#pending' tab 确认 / 禁用 / 充值"
    )
    throttle_key = f"needs_human:{channel_name}:{error_info.category.value}"
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(
            _send_alert_with_silence(msg, key=throttle_key, silence_seconds=3600)
        )
    except RuntimeError:
        logger.warning(f"[alerter fallback needs_human] {msg}")
    except Exception as e:
        logger.warning(f"_schedule_alert_with_info 调度失败: {e}")


async def _send_alert_with_silence(message: str, key: str | None = None, silence_seconds: float = 3600):
    """异步调 alerter.send_alert, 带自定义静默窗口"""
    try:
        from intelligence.alerter import send_alert
        await send_alert(message, key=key, silence_seconds=silence_seconds)
    except Exception as e:
        logger.warning(f"告警发送失败: {e}")


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
