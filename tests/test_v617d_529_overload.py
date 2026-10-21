"""V6.17d: MiniMax 529 "服务过载" 非标码处理 (2026-10-21)

背景: tier:vision 首选切到 minimax-m3.1-flash 后, 上游返回 HTTP 529
(非标"服务过载"码, 生产实证) ——
① routes 的 failover 白名单只有 (429,500,502,503,504) → 529 不 failover,
   过载直接打穿到客户端;
② error_classifier 判 unknown → 走兜底默认冷却, 告警层也不认。

修复: 529 与 503 同语义 — 分类 upstream_5xx (60s 冷却 + 300s 探测),
组路由 failover / embeddings failover / 告警三处白名单都补 529。
"""
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent


def test_classifier_maps_529_to_upstream_5xx():
    from gateway.error_classifier import classify
    info = classify(529, "service overloaded", None)
    assert info.category.value == "upstream_5xx", info.category.value
    assert info.needs_human is False
    assert info.suggested_cooldown_seconds == 60
    assert info.suggested_probe_after_seconds == 300


def test_529_in_failover_whitelists():
    routes_src = (PROJECT_ROOT / "gateway" / "routes.py").read_text(encoding="utf-8")
    assert "retryable_status = response.status_code in (429, 500, 502, 503, 504, 529)" in routes_src
    emb_src = (PROJECT_ROOT / "gateway" / "embeddings.py").read_text(encoding="utf-8")
    assert "_RETRYABLE_STATUS = (429, 500, 502, 503, 504, 529)" in emb_src
    proxy_src = (PROJECT_ROOT / "gateway" / "proxy.py").read_text(encoding="utf-8")
    assert proxy_src.count("(401, 403, 500, 502, 503, 504, 529)") == 2, \
        "流式/非流式两处告警白名单都应含 529"
