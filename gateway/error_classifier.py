"""V4.9 (2026-08-25): 上游错误分类器

把上游失败分类成 9 种 ErrorCategory, 每种带:
- needs_human: 是否需要人工干预 (quota / auth / model_unsupported 需人工)
- suggested_cooldown_seconds: 建议冷却时长
- suggested_probe_after_seconds: 多久后主动探活 (None = 不主动探)

分类优先级 (首个命中返回):
1. exception 优先于 status_code (exception 含更精准的网络层错误)
2. status_code 优先于 message 关键字
3. code 字段 (上游 error.code) 优先于 message 字段
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import httpx


class ErrorCategory(str, Enum):
    QUOTA_EXHAUSTED = "quota_exhausted"       # 余额/plan 耗尽 (需人工)
    AUTH_FAILED = "auth_failed"               # key 失效/过期 (需人工)
    RATE_LIMITED = "rate_limited"              # 临时限流 (自愈)
    MODEL_UNSUPPORTED = "model_unsupported"    # model 不支持 (可能自愈)
    CONTEXT_WINDOW = "context_window"          # 上下文超限 (客户端侧, 行为中性)
    NETWORK_TRANSIENT = "network_transient"    # 网络层错误 (自愈)
    UPSTREAM_5XX = "upstream_5xx"              # 上游 5xx (自愈但慢)
    CLIENT_ERROR = "client_error"              # 业务 4xx (不动 cooldown)
    UNKNOWN = "unknown"


@dataclass
class ErrorInfo:
    """分类结果"""
    category: ErrorCategory
    error_code: Optional[str]      # 上游 error.code (如 "1113", "insufficient_quota")
    error_msg: str                  # 上游 error.message (截断 500 字符)
    needs_human: bool               # 是否需要人工干预
    suggested_cooldown_seconds: int
    suggested_probe_after_seconds: Optional[int]  # None = 不主动探活


# === 关键字匹配 (regex, ignorecase) ===
# 注意: code 字段先匹配 (更精准), message 字段后匹配 (兜底)
_QUOTA_KEYWORDS = re.compile(
    r"(insufficient_quota|quota_exceeded|quota_near_limit|余额不足|无可用资源包|"
    r"已达上限|已达.+?上限|plan.+?(耗尽|用尽|到期|exhausted)|"
    r"free.+?tier.+?limit|billing.+?limit|不够|欠费|充值)",
    re.IGNORECASE,
)
_AUTH_KEYWORDS = re.compile(
    r"(invalid_api_key|incorrect.api.key|unauthorized|invalid.token|"
    r"key.+?expired|key.+?invalid|authentication.+?failed|forbidden|权限|鉴权失败|密钥无效)",
    re.IGNORECASE,
)
_MODEL_KEYWORDS = re.compile(
    r"(model_not_found|model.+?does.+?not.+?exist|model.+?not.+?available|"
    r"unsupported.model|invalid.model|model.+?decommissioned|模型不存在|模型不支持|模型已下线)",
    re.IGNORECASE,
)
# V6.x: 上下文超限 (deepseek CONTEXT_WINDOW_EXCEEDED / OpenAI context_length_exceeded 等)
# 用 haystack (code+message) 匹配 — deepseek 把标记放在 error.code 字段
_CONTEXT_WINDOW_KEYWORDS = re.compile(
    r"(context_window_exceeded|context_length_exceeded|context.?window|"
    r"context.?length|prompt.+?(too long|is too long|exceeds)|"
    r"上下文.+?(超限|过长|超出)|输入.+?(超限|过长))",
    re.IGNORECASE,
)

_ERR_MSG_MAX = 500


def _truncate(s: Optional[str], n: int = _ERR_MSG_MAX) -> str:
    if not s:
        return ""
    s = s.strip()
    return s[:n] if len(s) > n else s


def _extract_code_and_msg(response_body: Optional[str]) -> tuple[Optional[str], str]:
    """从 response body 提取 (error.code, error.message)
    支持两种格式:
    1. OpenAI 风格: {"error": {"code": "...", "message": "..."}}
    2. 国内厂商: {"code": "...", "message": "..."}
    3. 纯文本 (兜底)
    """
    if not response_body:
        return None, ""

    # 尝试 JSON 解析
    try:
        import json
        parsed = json.loads(response_body)
        if isinstance(parsed, dict):
            err = parsed.get("error")
            if isinstance(err, dict):
                code = err.get("code") or err.get("type")
                msg = err.get("message") or err.get("msg") or ""
                return (str(code) if code else None), _truncate(str(msg) if msg else "")
            # 国内厂商顶层 code/message
            code = parsed.get("code")
            msg = parsed.get("message") or ""
            if code or msg:
                return (str(code) if code else None), _truncate(str(msg) if msg else "")
    except (json.JSONDecodeError, ValueError, TypeError):
        pass

    return None, _truncate(response_body)


def classify(
    status_code: int,
    response_body: Optional[str] = None,
    exception: Optional[BaseException] = None,
) -> ErrorInfo:
    """分类上游错误

    Args:
        status_code: HTTP status code (异常时传 0)
        response_body: 上游 response text (可选)
        exception: 客户端抛的异常 (TimeoutException/ConnectError 等, 优先)

    Returns:
        ErrorInfo
    """
    # === 1. exception 优先 (网络层错误最准) ===
    if exception is not None:
        if isinstance(exception, (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError,
                                  httpx.ReadError, httpx.WriteError, httpx.PoolTimeout,
                                  ConnectionError, TimeoutError)):
            return ErrorInfo(
                category=ErrorCategory.NETWORK_TRANSIENT,
                error_code=None,
                error_msg=_truncate(str(exception) or exception.__class__.__name__),
                needs_human=False,
                suggested_cooldown_seconds=15,
                suggested_probe_after_seconds=60,
            )
        # 其他异常 (程序 bug) → unknown
        return ErrorInfo(
            category=ErrorCategory.UNKNOWN,
            error_code=None,
            error_msg=_truncate(str(exception) or exception.__class__.__name__),
            needs_human=False,
            suggested_cooldown_seconds=60,
            suggested_probe_after_seconds=300,
        )

    # === 2. 提取 code/message ===
    code, msg = _extract_code_and_msg(response_body)
    haystack = f"{code or ''} {msg}".strip()

    # === 3. code 字段优先匹配 (最准) ===
    if code:
        # quota 类 code (含 "1113" 国内厂商特殊码)
        if code in ("1113",) or _QUOTA_KEYWORDS.search(code):
            return ErrorInfo(
                category=ErrorCategory.QUOTA_EXHAUSTED,
                error_code=code,
                error_msg=msg,
                needs_human=True,
                suggested_cooldown_seconds=3600,
                suggested_probe_after_seconds=1800,
            )
        if code == "insufficient_quota" or "quota" in code.lower() and "exceed" in code.lower():
            return ErrorInfo(
                category=ErrorCategory.QUOTA_EXHAUSTED,
                error_code=code,
                error_msg=msg,
                needs_human=True,
                suggested_cooldown_seconds=3600,
                suggested_probe_after_seconds=1800,
            )
        if code in ("invalid_api_key", "unauthorized", "forbidden", "authentication_failed"):
            return ErrorInfo(
                category=ErrorCategory.AUTH_FAILED,
                error_code=code,
                error_msg=msg,
                needs_human=True,
                suggested_cooldown_seconds=86400,
                suggested_probe_after_seconds=None,  # 必须人工
            )
        if code in ("model_not_found", "model_unavailable", "invalid_model"):
            return ErrorInfo(
                category=ErrorCategory.MODEL_UNSUPPORTED,
                error_code=code,
                error_msg=msg,
                needs_human=True,
                suggested_cooldown_seconds=1800,
                suggested_probe_after_seconds=600,
            )

    # === 3.5 上下文超限 (code+message 联合匹配) ===
    # V6.x: deepseek 把 "CONTEXT_WINDOW_EXCEEDED" 放在 error.code, OpenAI 等放在 message,
    # 所以对 haystack (code+msg) 整体匹配, 放在 code 精确匹配之后、message 关键字之前。
    # 行为中性: 不冷却 / 不探活 / 不需人工 — 仅为将来 "超限时路由到更大窗口渠道" 留分类基础。
    if _CONTEXT_WINDOW_KEYWORDS.search(haystack):
        return ErrorInfo(
            category=ErrorCategory.CONTEXT_WINDOW,
            error_code=code,
            error_msg=msg,
            needs_human=False,
            suggested_cooldown_seconds=0,
            suggested_probe_after_seconds=None,
        )

    # === 4. message 关键字匹配 (兜底) ===
    if msg:
        if _QUOTA_KEYWORDS.search(msg):
            return ErrorInfo(
                category=ErrorCategory.QUOTA_EXHAUSTED,
                error_code=code,
                error_msg=msg,
                needs_human=True,
                suggested_cooldown_seconds=3600,
                suggested_probe_after_seconds=1800,
            )
        if _AUTH_KEYWORDS.search(msg):
            return ErrorInfo(
                category=ErrorCategory.AUTH_FAILED,
                error_code=code,
                error_msg=msg,
                needs_human=True,
                suggested_cooldown_seconds=86400,
                suggested_probe_after_seconds=None,
            )
        if _MODEL_KEYWORDS.search(msg):
            return ErrorInfo(
                category=ErrorCategory.MODEL_UNSUPPORTED,
                error_code=code,
                error_msg=msg,
                needs_human=True,
                suggested_cooldown_seconds=1800,
                suggested_probe_after_seconds=600,
            )

    # === 5. status_code fallback ===
    if status_code == 429:
        # 429 但 code/message 不匹配 quota → rate_limited
        return ErrorInfo(
            category=ErrorCategory.RATE_LIMITED,
            error_code=code,
            error_msg=msg,
            needs_human=False,
            suggested_cooldown_seconds=30,
            suggested_probe_after_seconds=60,
        )
    if status_code in (401, 403):
        return ErrorInfo(
            category=ErrorCategory.AUTH_FAILED,
            error_code=code,
            error_msg=msg,
            needs_human=True,
            suggested_cooldown_seconds=86400,
            suggested_probe_after_seconds=None,
        )
    if status_code in (400, 404):
        return ErrorInfo(
            category=ErrorCategory.MODEL_UNSUPPORTED if status_code == 404 else ErrorCategory.CLIENT_ERROR,
            error_code=code,
            error_msg=msg,
            needs_human=(status_code == 404),
            suggested_cooldown_seconds=1800 if status_code == 404 else 0,
            suggested_probe_after_seconds=600 if status_code == 404 else None,
        )
    if status_code in (500, 502, 503, 504):
        return ErrorInfo(
            category=ErrorCategory.UPSTREAM_5XX,
            error_code=code,
            error_msg=msg,
            needs_human=False,
            suggested_cooldown_seconds=60,
            suggested_probe_after_seconds=300,
        )
    if 400 <= status_code < 500:
        return ErrorInfo(
            category=ErrorCategory.CLIENT_ERROR,
            error_code=code,
            error_msg=msg,
            needs_human=False,
            suggested_cooldown_seconds=0,
            suggested_probe_after_seconds=None,
        )

    # === 6. 兜底 ===
    return ErrorInfo(
        category=ErrorCategory.UNKNOWN,
        error_code=code,
        error_msg=msg,
        needs_human=False,
        suggested_cooldown_seconds=60,
        suggested_probe_after_seconds=300,
    )