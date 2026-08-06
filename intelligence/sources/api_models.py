"""Phase 3.6 (2026-08-06) — 通过 OpenAI 兼容 /v1/models API 结构化抽取模型列表

背景:
- 之前 model_list 源用 HTML 抓取 + AI 解析 (8-38s/次, ~252K tokens/天)
- 国内 9 家主流厂商 (DeepSeek/Qwen/GLM/SiliconFlow/Volcengine/StepFun/SenseNova/
  Hunyuan/Kimi) 都支持 OpenAI 兼容 /v1/models API
- 改用 API 结构化抽取: 0.5-2s/次, 0 tokens, 100% 确定性

设计:
- fetch_models_via_api(): 单点函数, 任何厂商只要提供 base_url + 可选 api_key
- 错误分 FetchApiError + (status_code) 让上层做差异化处理:
  - 401/403 (无 key 或 key 无效) → 提示 user 配置 channel
  - 404 (endpoint 不存在) → 永久禁用该 source
  - 5xx / Timeout → 重试 3 次
- 解析时兼容两种返回结构:
  - 标准 OpenAI:  {"object": "list", "data": [{"id": "...", ...}]}
  - GLM/DeepSeek: {"data": [{"id": "..."}]} 或 {"model": [...]} (少数厂商)
"""

import asyncio
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


# Phase 3.6: 标准 OpenAI 兼容 /v1/models 路径
# 9 家国内厂商均支持:
# - DeepSeek:      https://api.deepseek.com/v1
# - Qwen (DashScope): https://dashscope.aliyuncs.com/compatible-mode/v1
# - GLM:           https://open.bigmodel.cn/api/paas/v4
# - SiliconFlow:   https://api.siliconflow.cn/v1
# - Volcengine Ark: https://ark.cn-beijing.volces.com/api/v3
# - StepFun:       https://api.stepfun.com/v1
# - SenseNova:     https://api.sensenova.cn/v1
# - Hunyuan:       https://api.hunyuan.cloud.tencent.com/v1
# - Moonshot/Kimi: https://api.moonshot.cn/v1


class FetchApiError(Exception):
    """API 调用失败 (含 status_code 让上层判断是否降级)"""

    def __init__(self, message: str, status_code: Optional[int] = None, url: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.url = url


def _normalize_response(json_data: dict) -> list[str]:
    """把厂商 /v1/models 响应统一抽到 list[model_id]

    兼容:
    - OpenAI 标准: {"object": "list", "data": [{"id": "..."}, ...]}
    - GLM / DeepSeek 变体: {"data": [{"id": "..."}]}
    - 极个别: {"model": [{"id": "..."}]}
    """
    # 优先 data 字段
    items = json_data.get("data")
    if items and isinstance(items, list):
        return [item["id"] for item in items if "id" in item]

    # 兜底 model 字段
    items = json_data.get("model")
    if items and isinstance(items, list):
        return [item["id"] for item in items if "id" in item]

    # 都拿不到
    return []


async def fetch_models_via_api(
    base_url: str,
    api_key: Optional[str] = None,
    timeout: float = 30.0,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    backoff: float = 2.0,
) -> list[str]:
    """通过 OpenAI 兼容 API 拉取模型 ID 列表

    Args:
        base_url: 厂商 base URL, 不含 /models 后缀 (e.g. "https://api.siliconflow.cn/v1")
        api_key: 鉴权 key, 传 None 表示不鉴权 (公开 endpoint)
        timeout: 单次请求超时秒数, 默认 30
        max_attempts: 重试次数 (仅 5xx/timeout 重试, 4xx 不重试)
        base_delay / backoff: 重试退避 (1s → 2s → 4s)

    Returns:
        list[str] — model_id 列表 (按厂商返回顺序)

    Raises:
        FetchApiError: 包含 status_code
            - 401/403: key 缺失或无效, 上层应提示 user 配置 channel
            - 404: endpoint 不存在, 上层应永久禁用 source
            - 5xx/timeout: 已重试 max_attempts 次仍失败
            - 其他: 网络异常 / 解析失败

    Examples:
        >>> await fetch_models_via_api("https://api.siliconflow.cn/v1")
        ['Qwen/Qwen2.5-7B-Instruct', 'deepseek-ai/DeepSeek-V2.5', ...]
    """
    # 去掉末尾 /, 自动补 /models
    base_url = base_url.rstrip("/")
    url = f"{base_url}/models"

    headers = {"User-Agent": "aimc/3.6 (model-list-fetcher)"}
    if api_key:
        # OpenAI 标准 Bearer 鉴权 (9 家厂商都支持)
        headers["Authorization"] = f"Bearer {api_key}"

    last_error: Optional[Exception] = None

    for attempt in range(1, max_attempts + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(url, headers=headers)

            # 4xx (除 408/429) 不重试 — 重试无意义, 直接 raise
            if 400 <= resp.status_code < 500 and resp.status_code not in (408, 429):
                # 尝试读 body 拿 detail
                detail = ""
                try:
                    body = resp.json()
                    if isinstance(body, dict) and "error" in body:
                        err = body["error"]
                        if isinstance(err, dict):
                            detail = err.get("message", "")
                        else:
                            detail = str(err)
                except Exception:
                    detail = resp.text[:200]
                raise FetchApiError(
                    f"HTTP {resp.status_code}: {detail or resp.reason_phrase}",
                    status_code=resp.status_code,
                    url=url,
                )

            # 2xx → 解析
            if resp.status_code == 200:
                data = resp.json()
                models = _normalize_response(data)
                if not models:
                    raise FetchApiError(
                        f"API 返回 200 但 data/model 字段为空或格式未知: "
                        f"keys={list(data.keys())[:5]}",
                        status_code=200,
                        url=url,
                    )
                if attempt > 1:
                    logger.info(f"API 重试成功 (attempt {attempt}/{max_attempts}): {url}")
                return models

            # 5xx → 视为可重试
            last_error = FetchApiError(
                f"HTTP {resp.status_code}: {resp.text[:200]}",
                status_code=resp.status_code,
                url=url,
            )

        except httpx.TimeoutException as e:
            last_error = FetchApiError(f"请求超时: {e}", url=url)
        except httpx.RequestError as e:
            last_error = FetchApiError(f"网络错误: {type(e).__name__}: {e}", url=url)
        except FetchApiError:
            # 4xx 直接 raise (不重试)
            raise
        except Exception as e:
            last_error = FetchApiError(f"未知错误: {type(e).__name__}: {e}", url=url)

        if attempt < max_attempts:
            delay = base_delay * (backoff ** (attempt - 1))
            logger.warning(
                f"API 失败 (attempt {attempt}/{max_attempts}): {url} "
                f"error={last_error}, {delay:.1f}s 后重试"
            )
            await asyncio.sleep(delay)

    # 全部失败
    logger.error(f"API 全部 {max_attempts} 次失败: {url}, last_error={last_error}")
    if last_error:
        raise last_error
    raise FetchApiError(f"全部 {max_attempts} 次失败且无错误记录", url=url)