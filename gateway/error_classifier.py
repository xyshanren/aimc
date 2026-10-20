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
from datetime import datetime, timedelta
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
    # V6.17 (2026-10-21): modelscope 每日免费额度耗尽 (429 无 code/type, message
    # 就一句 "insufficient balance", 生产实录) — 用户确认是当日额度用完非共享池
    # 抖动, 原掉进 429 裸兜底按 30s 瞬时限流反复重撞。
    r"insufficient\s+balance|"
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
    r"unsupported.model|invalid.model|model.+?decommissioned|模型不存在|模型不支持|模型已下线|"
    # V6.x (2026-10-20): SF 403 code=30003 "Model disabled." — 模型级禁用
    # (该模型对本账号不可用), 不是 key 失效。必须先于 401/403 兜底匹配,
    # 否则整账号按 auth_failed 判 needs_human 冷 24h, 换 key 也修不好。
    r"model.+?disabled|模型已禁用)",
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

# V6.7 (2026-09-17): 限流语义提示词 — 上游把 TPM 限流标成 insufficient_quota 时兜底。
# 生产实证 (2026-09-17): sensenova 同端点对不同账号返回不同 error.code
# (429001 / insufficient_quota, message 同为 "inference exceeds tpm/rpm limit",
# type 字段都是 "rate_limit_error"); modelscope 免费层 token-limit 429 也是
# code=insufficient_quota + message 指向 error-code#token-limit。
# 这类都是共享池吞吐限流 (分钟级自愈), 不是计费配额耗尽。
_RATE_LIMIT_HINT = re.compile(
    r"(rate.?limit|\btpm\b|\brpm\b|429001|token.?limit|too many requests|触发限流|请求过于频繁)",
    re.IGNORECASE,
)

# V6.17 (2026-10-21): 真套餐耗尽判别 — minimax 把**所有** 429 的 error.type 都标成
# rate_limit_error (含 Token Plan 用量上限 2056), 于是上面的限流兜底会抢在
# message 配额匹配之前 → 误判为瞬时限流 → 账号级冷却 + 指数退避, 接不上
# V6.15 配额恢复探测 → 上游 5h 窗口刷新后 AIMC 仍白冷最多 1h (生产实证
# 2026-10-21: minimax_01 冷却 2055s 未自清, 官方控制台额度已刷新)。
# 判别依据: "升级套餐 / 购买积分" 这类话术只可能出现在真实套餐配额耗尽,
# 吞吐限流 (TPM/RPM) 不会让用户升级套餐 — sensenova/modelscope 实证无此类话术。
_PLAN_EXHAUSTED_HINT = re.compile(
    r"(升级.{0,16}(套餐|plan)|购买积分|积分.{0,6}(补充|额度|不足)|"
    r"套餐.{0,8}(上限|额度|耗尽|用尽)|token\s*plan.{0,12}(上限|额度|耗尽|用尽))",
    re.IGNORECASE,
)

# V6.17: 套餐窗口类配额耗尽 (无绝对重置时间时) 的桶隔离时长 — 覆盖典型订阅
# 窗口 (minimax Token Plan 5h)。比窗口短的固定冷却会让桶在窗口耗尽期内反复
# 到期 → 重撞 → 升级 needs_human; 恢复探测会在窗口刷新后提前放行, 不靠冷却到期。
_PLAN_WINDOW_SECONDS = 5 * 3600

# V6.17b (2026-10-21): 每日额度耗尽话术 — modelscope 429 裸 message
# "insufficient balance" (无 code/type, 生产实录), 按天重置 → 冷却到下一午夜。
_DAILY_QUOTA_HINT = re.compile(r"insufficient\s+balance", re.IGNORECASE)

# V6.15 (2026-09-28): quota_exhausted 消息里的上游重置时间感知
# 生产实证: GLM coding plan 1308 "已达到 5 小时的使用上限。您的限额将在
# 2026-09-28 16:44:16 重置。" — 固定 3600s 冷却会在上游重置后继续空窗
# (16:44 重置但 AIMC 冷到 ~17:12)。解析到绝对重置时间 → 冷却到重置点+缓冲。
# 只认绝对 datetime (相对表述 "5小时后" 歧义大不解析); 须同时出现重置语义词,
# 防止配额消息里无关时间戳误伤。
_RESET_KEYWORD_RE = re.compile(r"(重置|恢复|刷新|reset)", re.IGNORECASE)
_RESET_DATETIME_RE = re.compile(
    r"(\d{4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?"
)
# 重置点缓冲: 贴着边界放行容易撞上上游状态未翻转, 多等 30s
_RESET_BUFFER_SECONDS = 30
# 解析下限/上限: 重置点已过 (时钟偏差) → 最小 60s 让路由自然复检;
# 异常远期时间 (如解析到年份错误) 封顶 7 天, 防止把桶永久封死
_RESET_MIN_SECONDS = 60
_RESET_MAX_SECONDS = 7 * 86400

_ERR_MSG_MAX = 500


def _truncate(s: Optional[str], n: int = _ERR_MSG_MAX) -> str:
    if not s:
        return ""
    s = s.strip()
    return s[:n] if len(s) > n else s


def _quota_reset_cooldown_seconds(msg: str, default_seconds: int) -> int:
    """V6.15: 从 quota_exhausted 消息解析上游重置时间 → 冷却到重置点 (+缓冲).

    生产实证 (2026-09-28): glm_01 "已达到 5 小时的使用上限。您的限额将在
    2026-09-28 16:44:16 重置。" 固定 3600s 冷却在 16:44 重置后仍空窗 28 分钟。
    解析不到 / 无重置语义词 / 时间非法 → 原默认值 (行为不变)。
    """
    if not msg or not _RESET_KEYWORD_RE.search(msg):
        return default_seconds
    m = _RESET_DATETIME_RE.search(msg)
    if not m:
        return default_seconds
    try:
        y, mo, d, h, mi = (int(m.group(i)) for i in range(1, 6))
        sec = int(m.group(6)) if m.group(6) else 0
        reset_dt = datetime(y, mo, d, h, mi, sec)
    except ValueError:
        return default_seconds
    delta = (reset_dt - datetime.now()).total_seconds() + _RESET_BUFFER_SECONDS
    if delta < _RESET_MIN_SECONDS:
        return _RESET_MIN_SECONDS
    return min(int(delta), _RESET_MAX_SECONDS)


def _quota_cooldown_seconds(msg: str) -> int:
    """配额类失败的冷却时长 — 绝对重置时间优先, 否则按类型取默认。

    V6.17 (2026-10-21): 套餐窗口类 (message 含升级套餐/购买积分话术) 无绝对
    重置时间时用 5h 默认, 覆盖典型订阅窗口 (minimax Token Plan 5h)。比窗口
    短的固定冷却会让桶在窗口耗尽期内反复到期 → 重撞 → 升级 needs_human;
    恢复探测会在窗口刷新后提前放行, 不依赖冷却到期。

    V6.17b: 每日额度类 (modelscope "insufficient balance") → 冷却到下一个
    本地午夜 (+缓冲)。日配额按天重置, 固定 3600s 会在白天反复到期重撞;
    若重置时刻假设有偏差, 由恢复探测 / 桶到期后路由自然复检自愈。
    """
    if _DAILY_QUOTA_HINT.search(msg or ""):
        now = datetime.now()
        tomorrow = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return max(
            _RESET_MIN_SECONDS,
            int((tomorrow - now).total_seconds()) + _RESET_BUFFER_SECONDS,
        )
    default = _PLAN_WINDOW_SECONDS if _PLAN_EXHAUSTED_HINT.search(msg or "") else 3600
    return _quota_reset_cooldown_seconds(msg, default)


def _extract_code_and_msg(response_body: Optional[str]) -> tuple[Optional[str], str, Optional[str]]:
    """从 response body 提取 (error.code, error.message, error.type)
    支持两种格式:
    1. OpenAI 风格: {"error": {"code": "...", "message": "...", "type": "..."}}
    2. 国内厂商: {"code": "...", "message": "..."}
    3. 纯文本 (兜底)
    """
    if not response_body:
        return None, "", None

    # 尝试 JSON 解析
    try:
        import json
        parsed = json.loads(response_body)
        if isinstance(parsed, dict):
            err = parsed.get("error")
            if isinstance(err, dict):
                code = err.get("code") or err.get("type")
                msg = err.get("message") or err.get("msg") or ""
                err_type = err.get("type")
                return (str(code) if code else None), _truncate(str(msg) if msg else ""), (
                    str(err_type) if err_type else None
                )
            # 国内厂商顶层 code/message
            code = parsed.get("code")
            msg = parsed.get("message") or ""
            if code or msg:
                return (str(code) if code else None), _truncate(str(msg) if msg else ""), None
    except (json.JSONDecodeError, ValueError, TypeError):
        pass

    return None, _truncate(response_body), None


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

    # === 2. 提取 code/message/type ===
    code, msg, err_type = _extract_code_and_msg(response_body)
    haystack = f"{code or ''} {err_type or ''} {msg}".strip()

    # === 2.5 V6.7: 限流语义兜底 (先于 code 精确匹配) ===
    # 429 + 报错文案/ type 带限流语义 → RATE_LIMITED, 即使 code 是
    # insufficient_quota (sensenova/modelscope 把共享池 TPM 限流标成配额错误,
    # 生产实证见 _RATE_LIMIT_HINT 注释)。真计费耗尽的 message 不含这些词,
    # 仍走下方 code 分支判 quota_exhausted。
    #
    # V6.17 (2026-10-21): 加真套餐耗尽豁免 — minimax 把所有 429 (含 Token Plan
    # 用量上限 2056) 的 error.type 都标 rate_limit_error, 上面"真计费耗尽的
    # message 不含这些词"的前提被证伪 (限流词在 type 里, 不在 message 里)。
    # 豁免条件: message 命中套餐耗尽话术 **且** 本身可命中配额关键字 ——
    # 只把"本该判配额"的情况让给 §4, 其余 429 分类不变。
    _msg = msg or ""
    _is_plan_exhausted = bool(
        _PLAN_EXHAUSTED_HINT.search(_msg) and _QUOTA_KEYWORDS.search(_msg)
    )
    if status_code == 429 and _RATE_LIMIT_HINT.search(haystack) and not _is_plan_exhausted:
        return ErrorInfo(
            category=ErrorCategory.RATE_LIMITED,
            error_code=code,
            error_msg=msg,
            needs_human=False,
            suggested_cooldown_seconds=300,
            suggested_probe_after_seconds=600,
        )

    # === 3. code 字段优先匹配 (最准) ===
    if code:
        # quota 类 code (含 "1113" 国内厂商特殊码)
        if code in ("1113",) or _QUOTA_KEYWORDS.search(code):
            return ErrorInfo(
                category=ErrorCategory.QUOTA_EXHAUSTED,
                error_code=code,
                error_msg=msg,
                needs_human=True,
                suggested_cooldown_seconds=_quota_cooldown_seconds(msg),
                suggested_probe_after_seconds=1800,
            )
        if code == "insufficient_quota" or "quota" in code.lower() and "exceed" in code.lower():
            return ErrorInfo(
                category=ErrorCategory.QUOTA_EXHAUSTED,
                error_code=code,
                error_msg=msg,
                needs_human=True,
                suggested_cooldown_seconds=_quota_cooldown_seconds(msg),
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
                suggested_cooldown_seconds=_quota_cooldown_seconds(msg),
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
        # V6.6.x (2026-09-16): TPM/RPM 类限流是共享池吞吐约束而非瞬时过载 —
        # sensenova 实测 63% 的 429 后面跟着 429, 完全静默 5 分钟也不恢复,
        # 30s 基数解冻即重撞; 提到 300s (指数退避不变, 仍封顶 health_probe_max_backoff)。
        # 其他瞬时 429 (如 glm 1305 过载波纹) 保持 30s 快速自愈。
        _h = haystack.lower()
        if "429001" in _h or "tpm" in _h or "rpm" in _h:
            return ErrorInfo(
                category=ErrorCategory.RATE_LIMITED,
                error_code=code,
                error_msg=msg,
                needs_human=False,
                suggested_cooldown_seconds=300,
                suggested_probe_after_seconds=600,
            )
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