"""Phase 3.6 (2026-08-06) — 7 个新 model_list 源 + scheduler 跳过空 url 测试

覆盖:
1. 7 个新 source 都能正确初始化 (api_endpoint/api_key_from 设置)
2. get_all_sources() 返回 19 个 source (原 12 + 新 7)
3. model_list 类型 source 共 9 个 (原 2 + 新 7)
4. scheduler._run_source_task: API 失败 + url="" → 跳过不抛错
5. scheduler._run_source_task: API 失败 + url 存在 → 降级 HTML

跑法: python tests/test_phase3_6_extra_sources.py
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ============================================================
# Mock config
# ============================================================
def _install_mock_config():
    import types

    class _MockSettings:
        openai_api_key = "sk-test"
        openai_base_url = "https://mock.local/v1"
        parse_model = "gpt-4o-mini"
        parse_confidence_threshold = 0.6
        crawl_enabled = True
        aimc_master_key = "0" * 64
        route_refresh_interval = 600
        db_url = "sqlite+aiosqlite:///:memory:"
        db_path = "./data/aimc.db"

    sys.modules.setdefault("config", types.ModuleType("config"))
    sys.modules["config"].settings = _MockSettings()


_install_mock_config()


# ============================================================
# Test 1: 7 个新 source 配置正确
# ============================================================
def test_new_sources_configured():
    print("\n=== Test 1: 7 个新 model_list source 配置 ===")
    from intelligence.sources.deepseek import DeepSeekModelListSource
    from intelligence.sources.qwen import QwenModelListSource
    from intelligence.sources.glm import GLMModelListSource
    from intelligence.sources.model_lists_extra import (
        StepFunModelListSource,
        SenseNovaModelListSource,
        HunyuanModelListSource,
        MoonshotModelListSource,
    )
    from intelligence.sources.base import SourceType

    cases = [
        # (class, expected_provider, expected_api_endpoint, expected_api_key_from)
        (DeepSeekModelListSource, "deepseek", "https://api.deepseek.com/v1", "deepseek"),
        (QwenModelListSource, "qwen", "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen"),
        (GLMModelListSource, "glm", "https://open.bigmodel.cn/api/paas/v4", "glm"),
        (StepFunModelListSource, "stepfun", "https://api.stepfun.com/v1", "stepfun"),
        (SenseNovaModelListSource, "sensenova", "https://token.sensenova.cn/v1", "sensenova"),
        (HunyuanModelListSource, "hunyuan", "https://api.hunyuan.cloud.tencent.com/v1", "hunyuan"),
        (MoonshotModelListSource, "moonshot", "https://api.moonshot.cn/v1", "moonshot"),
    ]
    for cls, exp_provider, exp_api, exp_key_from in cases:
        s = cls()
        assert s.provider == exp_provider, (
            f"{cls.__name__}: provider 应 {exp_provider}, 实际 {s.provider}"
        )
        assert s.api_endpoint == exp_api, (
            f"{cls.__name__}: api_endpoint 应 {exp_api}, 实际 {s.api_endpoint}"
        )
        assert s.api_key_from == exp_key_from
        assert s.source_type == SourceType.MODEL_LIST
        assert s.poll_interval == 86400, "model_list 频率应为 1 天"
        # 4 个新厂商 source 不配 HTML url
        if cls.__name__ in ("StepFunModelListSource", "SenseNovaModelListSource",
                              "HunyuanModelListSource", "MoonshotModelListSource"):
            assert s.url == "", f"{cls.__name__} 应 url='' (API-only), 实际 {s.url!r}"

    print(f"  [OK] 7 个新 source 全部配置正确")


# ============================================================
# Test 2: get_all_sources 返回 37 个 (V4.8.3 后全量快照)
# ============================================================
def test_get_all_sources_count():
    print("\n=== Test 2: get_all_sources() 返回 37 个 source ===")
    from intelligence.sources import get_all_sources

    srcs = get_all_sources()
    assert len(srcs) == 37, f"V4.8.3 后应 37 个 source, 实际 {len(srcs)}"

    by_type = {}
    for s in srcs:
        by_type.setdefault(s.source_type, []).append(s.provider)

    print(f"  [OK] 总数 37: {dict((k, len(v)) for k, v in by_type.items())}")
    for st, providers in sorted(by_type.items()):
        print(f"        {st}: {providers}")


# ============================================================
# Test 3: model_list 类型 source 共 9 个
# ============================================================
def test_model_list_sources_count():
    print("\n=== Test 3: model_list source 共 9 个 ===")
    from intelligence.sources import get_all_sources

    ml_sources = [s for s in get_all_sources() if s.source_type == "model_list"]
    assert len(ml_sources) == 9, f"应 9 个 model_list source, 实际 {len(ml_sources)}"

    providers = sorted([s.provider for s in ml_sources])
    expected = sorted(["siliconflow", "volcengine", "deepseek", "qwen", "glm",
                        "stepfun", "sensenova", "hunyuan", "moonshot"])
    assert providers == expected, f"应 {expected}, 实际 {providers}"

    print(f"  [OK] 9 个 model_list: {providers}")


# ============================================================
# Test 4: scheduler 无 API key + url='' → 静默跳过 (V4.8.3)
# ============================================================
def test_scheduler_skip_empty_url_on_api_fail():
    """V4.8.3: api_endpoint 配了但无可用 API key 且 url='' → 静默跳过, 不告警

    旧逻辑 (2026-08 前): API 失败 → handle_fetch_error → 飞书告警刷屏
    新逻辑 (V4.8.3):     无 key + 无 url → return, 不调 API 也不告警
    """
    print("\n=== Test 4: scheduler 无 API key + url='' → 静默跳过 ===")
    from intelligence.scheduler import _run_source_task
    from intelligence.sources.base import IntelSourceBase

    # 模拟 StepFun source (url="", api_endpoint="https://api.stepfun.com/v1")
    source = IntelSourceBase(
        provider="stepfun",
        url="",
        source_type="model_list",
        poll_interval=86400,
        api_endpoint="https://api.stepfun.com/v1",
        api_key_from="stepfun",
    )

    api_called = {"count": 0}
    fetch_called = {"count": 0}
    fetch_err_called = {"msg": None}

    async def _mock_fetch_api(*a, **kw):
        api_called["count"] += 1
        return []

    async def _mock_fetch_page(*a, **kw):
        fetch_called["count"] += 1
        return None, "should not be called"

    async def _mock_handle_fetch_error(s, err):
        fetch_err_called["msg"] = err

    async def _mock_resolve_api_key(s):
        return None  # 模拟无可用 key

    async def _mock_has_changed(s, content):
        return True

    async def _mock_handle_parse_result(content, source):
        pass

    with patch("intelligence.sources.api_models.fetch_models_via_api", _mock_fetch_api), \
         patch("intelligence.fetcher.fetch_page_with_error", _mock_fetch_page), \
         patch("intelligence.healer.handle_fetch_error", _mock_handle_fetch_error), \
         patch("intelligence.scheduler._resolve_api_key", _mock_resolve_api_key), \
         patch("intelligence.differ.has_changed", _mock_has_changed), \
         patch("intelligence.healer.handle_parse_result", _mock_handle_parse_result):
        asyncio.run(_run_source_task(source))

    # 验证: 无 key + 无 url → 静默跳过, 不调 API
    assert api_called["count"] == 0, (
        f"无 API key 时不应调 fetch_models_via_api, 实际调用 {api_called['count']} 次"
    )
    # 验证: 不应调用 fetch_page_with_error (因为 url="")
    assert fetch_called["count"] == 0, (
        f"url='' 时不应走 HTML fallback, 实际调用 {fetch_called['count']} 次"
    )
    # 验证: 不应调用 handle_fetch_error (V4.8.3: 静默跳过, 不告警)
    assert fetch_err_called["msg"] is None, (
        f"静默跳过不应调 handle_fetch_error, 实际调用了: {fetch_err_called['msg']!r}"
    )
    print("  [OK] 无 key + url='' → 静默跳过, 不调 API/HTML/告警")


# ============================================================
# Test 5: scheduler API 失败 + url 存在 → 降级 HTML
# ============================================================
def test_scheduler_fallback_to_html_when_url_set():
    print("\n=== Test 5: scheduler API 失败 + url 存在 → 降级 HTML ===")
    from intelligence.scheduler import _run_source_task
    from intelligence.sources.api_models import FetchApiError
    from intelligence.sources.base import IntelSourceBase

    # 模拟 SiliconFlow (url 有值, api_endpoint 有值)
    source = IntelSourceBase(
        provider="siliconflow",
        url="https://docs.siliconflow.cn/cn-userguide/quickstart",
        source_type="model_list",
        poll_interval=86400,
        api_endpoint="https://api.siliconflow.cn/v1",
        api_key_from="siliconflow",
    )

    fetch_html_called = {"count": 0}

    async def _mock_fetch_api(*a, **kw):
        raise FetchApiError("HTTP 500: server error", status_code=500)

    async def _mock_fetch_page(*a, **kw):
        fetch_html_called["count"] += 1
        return "<html><body>siliconflow docs</body></html>", None

    async def _mock_handle_fetch_error(s, err):
        pass

    async def _mock_resolve_api_key(s):
        return None

    async def _mock_has_changed(s, content):
        return True

    async def _mock_handle_parse_result(content, source):
        # 验证 content 是 HTML (不是 JSON)
        # 但因为 is_promotion=False 走 AI 解析, 内容应是 HTML 字符串
        assert "<html>" in content or "siliconflow" in content

    with patch("intelligence.sources.api_models.fetch_models_via_api", _mock_fetch_api), \
         patch("intelligence.fetcher.fetch_page_with_error", _mock_fetch_page), \
         patch("intelligence.healer.handle_fetch_error", _mock_handle_fetch_error), \
         patch("intelligence.scheduler._resolve_api_key", _mock_resolve_api_key), \
         patch("intelligence.differ.has_changed", _mock_has_changed), \
         patch("intelligence.healer.handle_parse_result", _mock_handle_parse_result):
        asyncio.run(_run_source_task(source))

    # 验证: HTML fallback 被调用
    assert fetch_html_called["count"] == 1, (
        f"url 存在时应调用 HTML fallback, 实际 {fetch_html_called['count']} 次"
    )
    print(f"  [OK] URL 存在时降级 HTML, 调用 {fetch_html_called['count']} 次")


# ============================================================
# Main
# ============================================================
def main():
    print("=" * 60)
    print("Phase 3.6 — 7 个新 model_list source 测试")
    print("=" * 60)

    test_new_sources_configured()
    test_get_all_sources_count()
    test_model_list_sources_count()
    test_scheduler_skip_empty_url_on_api_fail()
    test_scheduler_fallback_to_html_when_url_set()

    print("\n" + "=" * 60)
    print("ALL 5 TESTS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    main()