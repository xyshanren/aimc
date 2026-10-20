"""V4.9 (2026-08-25): error_classifier 单元测试
覆盖 8 个 ErrorCategory + 关键字 + status_code + exception 分支

跑法:
    python tests/test_error_classifier.py
"""
import sys
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from gateway.error_classifier import ErrorCategory, ErrorInfo, classify


_passed = 0
_failed = 0


def check(desc, got, expected):
    global _passed, _failed
    if got == expected:
        _passed += 1
        print(f"  PASS  {desc}")
    else:
        _failed += 1
        print(f"  FAIL  {desc}  got={got!r}  expected={expected!r}")


# ===== 1. quota_exhausted =====
def test_quota_chinese_vendor():
    """国内厂商 1113 余额不足"""
    info = classify(429, '{"code":"1113","message":"余额不足或无可用资源包,请充值。"}')
    check("category", info.category, ErrorCategory.QUOTA_EXHAUSTED)
    check("needs_human", info.needs_human, True)
    check("cooldown", info.suggested_cooldown_seconds, 3600)
    check("probe_after", info.suggested_probe_after_seconds, 1800)
    check("error_code", info.error_code, "1113")


def test_quota_openai_style():
    """OpenAI 风格 insufficient_quota"""
    info = classify(429, '{"error":{"code":"insufficient_quota","message":"You exceeded your current quota"}}')
    check("category", info.category, ErrorCategory.QUOTA_EXHAUSTED)
    check("needs_human", info.needs_human, True)
    check("error_code", info.error_code, "insufficient_quota")


def test_quota_plan_exhausted():
    """minimax TokenPlan 耗尽场景"""
    info = classify(429, '{"error":{"message":"已达到 Token Plan 用量上限"}}')
    check("category", info.category, ErrorCategory.QUOTA_EXHAUSTED)
    check("needs_human", info.needs_human, True)


def test_quota_plan_exhausted_minimax_real_body():
    """V6.17: 生产真实响应体 (2026-10-21 request_transcripts 实录)。

    minimax 把**所有** 429 的 error.type 都标成 rate_limit_error, 含 Token
    Plan 用量上限 2056 — 命中 V6.7 限流兜底会抢在 message 配额匹配之前, 误判
    为瞬时限流 → 账号级冷却 + 指数退避, 接不上 V6.15 配额恢复探测 (上游 5h
    窗口刷新后 AIMC 仍白冷最多 1h)。必须判配额耗尽 + 5h 窗口桶隔离。
    """
    body = (
        '{"type":"error","error":{"type":"rate_limit_error","message":'
        '"已达到 Token Plan 用量上限：请升级 Token Plan 套餐或购买积分补充用量。 (2056)",'
        '"http_code":"429"},"request_id":"0726c2f2"}'
    )
    info = classify(429, body)
    check("category", info.category, ErrorCategory.QUOTA_EXHAUSTED)
    check("needs_human", info.needs_human, True)
    check("cooldown 5h 订阅窗口", info.suggested_cooldown_seconds, 18000)
    check("probe_after", info.suggested_probe_after_seconds, 1800)


def test_rate_limited_tpm_wording_not_touched_by_plan_exemption():
    """V6.17 回归: 吞吐限流 (TPM/RPM) 即使 type 标 rate_limit_error 仍走限流路径。"""
    body = '{"error":{"type":"rate_limit_error","message":"inference exceeds tpm/rpm limit"}}'
    info = classify(429, body)
    check("category", info.category, ErrorCategory.RATE_LIMITED)
    check("cooldown 300s", info.suggested_cooldown_seconds, 300)
    check("needs_human", info.needs_human, False)


# ===== 2. auth_failed =====
def test_auth_401():
    info = classify(401, '{"error":{"message":"Invalid API key"}}')
    check("category", info.category, ErrorCategory.AUTH_FAILED)
    check("needs_human", info.needs_human, True)
    check("probe_after (None)", info.suggested_probe_after_seconds, None)
    check("cooldown 24h", info.suggested_cooldown_seconds, 86400)


def test_auth_403():
    info = classify(403, '{"error":{"message":"Forbidden"}}')
    check("category", info.category, ErrorCategory.AUTH_FAILED)


def test_auth_code_match():
    """code 字段匹配 invalid_api_key"""
    info = classify(401, '{"error":{"code":"invalid_api_key","message":"..."}}')
    check("error_code", info.error_code, "invalid_api_key")
    check("category", info.category, ErrorCategory.AUTH_FAILED)


# ===== 3. rate_limited (429 但非 quota) =====
def test_rate_limited_429_no_quota_keyword():
    info = classify(429, '{"error":{"message":"Too Many Requests, slow down"}}')
    check("category", info.category, ErrorCategory.RATE_LIMITED)
    check("needs_human", info.needs_human, False)
    check("cooldown 30s", info.suggested_cooldown_seconds, 30)


# ===== 4. model_unsupported =====
def test_model_404():
    info = classify(404, '{"error":{"message":"Model not found"}}')
    check("category", info.category, ErrorCategory.MODEL_UNSUPPORTED)
    check("needs_human", info.needs_human, True)


def test_model_400_with_code():
    """400 + model_not_found code"""
    info = classify(400, '{"error":{"code":"model_not_found","message":"..."}}')
    check("category", info.category, ErrorCategory.MODEL_UNSUPPORTED)
    check("needs_human", info.needs_human, True)


def test_model_disabled_sf_30003():
    """生产实证 (2026-10-20): SF 403 code=30003 "Model disabled." 是模型级禁用,
    不是 key 失效 — 归 MODEL_UNSUPPORTED, 不得走 401/403 兜底判 auth_failed
    (曾致整账号 needs_human 冷 24h, 换 key 修不好)"""
    info = classify(403, '{"code":30003,"message":"Model disabled.","data":null}')
    check("category", info.category, ErrorCategory.MODEL_UNSUPPORTED)
    check("error_code", info.error_code, "30003")
    check("needs_human", info.needs_human, True)
    check("cooldown", info.suggested_cooldown_seconds, 1800)


# ===== 5. network_transient =====
def test_network_timeout():
    """TimeoutException → network_transient"""
    info = classify(0, None, httpx.TimeoutException("read timeout"))
    check("category", info.category, ErrorCategory.NETWORK_TRANSIENT)
    check("needs_human", info.needs_human, False)
    check("cooldown 15s", info.suggested_cooldown_seconds, 15)


def test_network_connect_error():
    info = classify(0, None, httpx.ConnectError("connection refused"))
    check("category", info.category, ErrorCategory.NETWORK_TRANSIENT)


def test_network_remote_protocol_error():
    info = classify(0, None, httpx.RemoteProtocolError("server closed connection"))
    check("category", info.category, ErrorCategory.NETWORK_TRANSIENT)


# ===== 6. upstream_5xx =====
def test_5xx_500():
    info = classify(500, '{"error":{"message":"Internal Server Error"}}')
    check("category", info.category, ErrorCategory.UPSTREAM_5XX)
    check("needs_human", info.needs_human, False)


def test_5xx_502():
    info = classify(502, 'Bad Gateway')
    check("category", info.category, ErrorCategory.UPSTREAM_5XX)


def test_5xx_504():
    info = classify(504, '')
    check("category", info.category, ErrorCategory.UPSTREAM_5XX)


# ===== 7. client_error =====
def test_client_400_no_model_keyword():
    info = classify(400, '{"error":{"message":"Invalid request: missing field"}}')
    check("category", info.category, ErrorCategory.CLIENT_ERROR)
    check("needs_human", info.needs_human, False)
    check("cooldown 0 (不进)", info.suggested_cooldown_seconds, 0)


def test_client_422():
    info = classify(422, '{"error":{"message":"Unprocessable"}}')
    check("category", info.category, ErrorCategory.CLIENT_ERROR)


# ===== 8. unknown =====
def test_unknown_generic_exception():
    """非 httpx 异常 → unknown"""
    info = classify(0, None, ValueError("something weird"))
    check("category", info.category, ErrorCategory.UNKNOWN)


# ===== 9. context_window (V6.x) =====
def test_context_window_deepseek_code():
    """deepseek 把标记放在 error.code 字段 → 必须走 haystack (code+msg) 匹配"""
    info = classify(400, '{"error":{"code":"CONTEXT_WINDOW_EXCEEDED"}}')
    check("category", info.category, ErrorCategory.CONTEXT_WINDOW)
    check("needs_human", info.needs_human, False)
    check("cooldown 0 (不进)", info.suggested_cooldown_seconds, 0)
    check("probe_after (None)", info.suggested_probe_after_seconds, None)
    check("error_code", info.error_code, "CONTEXT_WINDOW_EXCEEDED")


def test_context_window_openai_message():
    """OpenAI 风格 message 关键字"""
    info = classify(400, '{"error":{"message":"This models maximum context length is 32768 tokens"}}')
    check("category", info.category, ErrorCategory.CONTEXT_WINDOW)
    check("needs_human", info.needs_human, False)


def test_client_400_without_context_keywords():
    """普通 400 无上下文关键字 → 仍归 client_error (行为不变)"""
    info = classify(400, '{"error":{"message":"Invalid request: missing field"}}')
    check("category", info.category, ErrorCategory.CLIENT_ERROR)


# ===== 边界 case =====
def test_empty_body_429():
    """429 但没 body → rate_limited"""
    info = classify(429, "")
    check("category", info.category, ErrorCategory.RATE_LIMITED)


def test_malformed_body_500():
    """500 + 乱码 body → upstream_5xx"""
    info = classify(500, "<html>500 error</html>")
    check("category", info.category, ErrorCategory.UPSTREAM_5XX)


def test_chinese_keyword_500():
    """500 + 中文关键字 (不太可能, 但兜底) → upstream_5xx 不被关键字截胡"""
    # 这里假设 message 不包含 quota/auth 关键字
    info = classify(500, '{"error":{"message":"服务器内部错误"}}')
    check("category", info.category, ErrorCategory.UPSTREAM_5XX)


def test_code_priority_over_message():
    """code 字段 (精准) 优先于 message 字段 (兜底)
    即使 message 含 quota, 只要 code 是 quota 相关, 就归 quota_exhausted
    """
    info = classify(429, '{"error":{"code":"rate_limit","message":"余额不足"}}')
    # 实际规则: code 字段先匹配 rate_limit (不在 quota 集合) → rate_limited
    # 然后 message 关键字匹配 quota → 升级为 quota_exhausted
    # 测试 message 关键字升级路径
    info2 = classify(429, '{"error":{"code":"unknown","message":"余额不足"}}')
    check("message quota 升级", info2.category, ErrorCategory.QUOTA_EXHAUSTED)


def test_truncate_long_message():
    """长 message 截断到 500"""
    long_msg = "x" * 1000
    info = classify(500, f'{{"error":{{"message":"{long_msg}"}}}}')
    check("truncated len=500", len(info.error_msg), 500)


def test_openai_vs_chinese_format():
    """两种格式都能解析"""
    info1 = classify(429, '{"error":{"code":"1113","message":"余额不足"}}')  # OpenAI 嵌套
    info2 = classify(429, '{"code":"1113","message":"余额不足"}}')  # 国内厂商顶层
    check("OpenAI 嵌套", info1.category, ErrorCategory.QUOTA_EXHAUSTED)
    check("国内厂商顶层", info2.category, ErrorCategory.QUOTA_EXHAUSTED)


def main():
    tests = [
        test_quota_chinese_vendor,
        test_quota_openai_style,
        test_quota_plan_exhausted,
        test_quota_plan_exhausted_minimax_real_body,
        test_rate_limited_tpm_wording_not_touched_by_plan_exemption,
        test_auth_401,
        test_auth_403,
        test_auth_code_match,
        test_rate_limited_429_no_quota_keyword,
        test_model_404,
        test_model_400_with_code,
        test_model_disabled_sf_30003,
        test_network_timeout,
        test_network_connect_error,
        test_network_remote_protocol_error,
        test_5xx_500,
        test_5xx_502,
        test_5xx_504,
        test_client_400_no_model_keyword,
        test_client_422,
        test_unknown_generic_exception,
        test_context_window_deepseek_code,
        test_context_window_openai_message,
        test_client_400_without_context_keywords,
        test_empty_body_429,
        test_malformed_body_500,
        test_chinese_keyword_500,
        test_code_priority_over_message,
        test_truncate_long_message,
        test_openai_vs_chinese_format,
    ]
    for t in tests:
        print(f"\n--- {t.__name__} ---")
        try:
            t()
        except Exception as e:
            global _failed
            _failed += 1
            print(f"  EXCEPTION: {e}")

    print(f"\n=== 结果: {_passed} passed, {_failed} failed ===")
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())