"""Phase 4 v4.2 (2026-08-06): 千问AI 聚合平台情报源单元测试

覆盖 6 个验证点:
1. QianwenAISource 基础属性: provider=qianwenai, source_type=PRICE_AGGREGATOR
2. poll_interval = 86400 (1 天, 跟其他 price_aggregator 错开)
3. 完整 URL 含 ?source_channel=hy_qwen&utm_content=g_1000415132 UTM 参数
4. wait_until=networkidle (SPA 渲染)
5. api_endpoint=None (无公开 /v1/models, 走 HTML 抓取)
6. 注册到 get_all_sources() 共 20 个 source (6 web+9 model+3 price+2 bench)

跑法: python tests/test_qianwenai_source.py
"""

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ============================================================
# Test 1: QianwenAISource 基础属性
# ============================================================
def test_qianwenai_source_basic_attrs():
    """验证 QianwenAISource 基本配置正确"""
    print("\n=== Test 1: QianwenAISource 基础属性 ===")
    from intelligence.sources.qianwenai import QianwenAISource
    from intelligence.sources.base import SourceType, DEFAULT_POLL_INTERVALS

    src = QianwenAISource()

    # provider 标识正确
    assert src.provider == "qianwenai", f"应 provider=qianwenai, 实际 {src.provider}"

    # source_type 是聚合平台
    assert src.source_type == SourceType.PRICE_AGGREGATOR, (
        f"应 PRICE_AGGREGATOR, 实际 {src.source_type}"
    )

    # 不配 api_endpoint (HTML 抓取路径)
    assert src.api_endpoint is None, (
        f"千问AI 无公开 /v1/models, 应 api_endpoint=None, 实际 {src.api_endpoint}"
    )
    assert src.api_key_from is None

    # poll_interval = 1 天
    assert src.poll_interval == 86400, f"应 1 天=86400s, 实际 {src.poll_interval}"

    # 跟 SourceType 默认值对齐 (86400 != DEFAULT_POLL_INTERVALS[PRICE_AGGREGATOR] 的 21600)
    # 这是有意的: 千问AI 频次比 siliconflow/openrouter 低 (1 天 vs 6 小时)
    default_pa = DEFAULT_POLL_INTERVALS[SourceType.PRICE_AGGREGATOR]
    assert src.poll_interval != default_pa, (
        "千问AI 应比默认 price_aggregator 频率低 (1 天 vs 6h 错峰)"
    )

    # wait_until SPA 渲染 (仿 openrouter)
    assert src.wait_until == "networkidle", f"应 networkidle, 实际 {src.wait_until}"
    assert src.wait_for == "main"
    assert src.wait_for_timeout == 15000

    # 启用状态
    assert src.enabled is True

    print(f"  [OK] provider={src.provider}, type={src.source_type}, "
          f"poll={src.poll_interval}s, networkidle, no api_endpoint")


# ============================================================
# Test 2: URL 含 UTM 参数
# ============================================================
def test_qianwenai_url_full_utm():
    """验证 URL 含完整 UTM 参数 (用户提供的入口链接)"""
    print("\n=== Test 2: URL 含完整 UTM 参数 ===")
    from intelligence.sources.qianwenai import QianwenAISource

    src = QianwenAISource()

    expected_url = (
        "https://www.qianwenai.com/"
        "?source_channel=hy_qwen"
        "&utm_content=g_1000415132"
    )

    assert src.url == expected_url, (
        f"URL 应等于 {expected_url!r}, 实际 {src.url!r}"
    )

    # 包含必要参数 (向后兼容, UTM 可能扩展)
    assert "source_channel=hy_qwen" in src.url, "应含 source_channel=hy_qwen"
    assert "utm_content=" in src.url, "应含 utm_content 参数"

    print(f"  [OK] URL={src.url}")


# ============================================================
# Test 3: get_all_sources 注册 (共 19 个, v4.1 后是 19 — 等等让我确认)
# ============================================================
def test_qianwenai_registered_in_get_all_sources():
    """验证 QianwenAISource 已被 __init__.py 注册, 共 N 个 source"""
    print("\n=== Test 3: 注册到 get_all_sources() ===")
    from intelligence.sources import get_all_sources
    from intelligence.sources.qianwenai import QianwenAISource

    sources = get_all_sources()

    # 找 qianwenai
    qianwenai_sources = [s for s in sources if s.provider == "qianwenai"]
    assert len(qianwenai_sources) == 1, (
        f"应 1 个 qianwenai 源, 实际 {len(qianwenai_sources)}"
    )

    # 类型正确
    qianwenai_src = qianwenai_sources[0]
    assert isinstance(qianwenai_src, QianwenAISource)
    assert qianwenai_src.source_type == "price_aggregator"

    # 总 source 数 (Phase 4 v4.2 应 = 19)
    # Phase 3.6 base = 18 (6 web/ann + 9 model + 2 price + 2 bench)
    # v4.2 +1 qianwenai → 19
    assert len(sources) == 20, (
        f"v4.2 后应 20 个 source, 实际 {len(sources)}"
    )

    print(f"  [OK] get_all_sources 共 {len(sources)} 个, qianwenai 已注册")


# ============================================================
# Test 4: get_sources_by_type 隔离
# ============================================================
def test_qianwenai_classified_as_price_aggregator():
    """验证 get_sources_by_type("price_aggregator") 包含 qianwenai"""
    print("\n=== Test 4: get_sources_by_type 分类正确 ===")
    from intelligence.sources import get_sources_by_type

    pa_sources = get_sources_by_type("price_aggregator")
    providers = sorted(s.provider for s in pa_sources)

    # 3 个 price_aggregator (siliconflow, openrouter, qianwenai)
    assert providers == ["openrouter", "qianwenai", "siliconflow"], (
        f"应 [openrouter, qianwenai, siliconflow], 实际 {providers}"
    )

    print(f"  [OK] price_aggregator 分类: {providers}")


# ============================================================
# Test 5: get_source_by_provider
# ============================================================
def test_get_source_by_provider_qianwenai():
    """验证 get_source_by_provider 能查到 qianwenai"""
    print("\n=== Test 5: get_source_by_provider 查找 ===")
    from intelligence.sources import get_source_by_provider

    sources = get_source_by_provider("qianwenai")
    assert len(sources) == 1
    assert sources[0].provider == "qianwenai"

    print(f"  [OK] get_source_by_provider('qianwenai') = {len(sources)} 个")


# ============================================================
# Test 6: 跟其他聚合源频次错开 (避免资源争抢)
# ============================================================
def test_qianwenai_poll_interval_off_peak():
    """验证 qianwenai 跟 siliconflow/openrouter 错峰 (1 天 vs 6 小时)"""
    print("\n=== Test 6: 错峰策略 ===")
    from intelligence.sources import get_sources_by_type

    pa_sources = get_sources_by_type("price_aggregator")
    by_provider = {s.provider: s.poll_interval for s in pa_sources}

    siliconflow_poll = by_provider["siliconflow"]
    openrouter_poll = by_provider["openrouter"]
    qianwenai_poll = by_provider["qianwenai"]

    # 错峰: qianwenai (1 天) vs siliconflow/openrouter (6 小时)
    assert qianwenai_poll == 86400, f"qianwenai 应 1 天 (86400), 实际 {qianwenai_poll}"
    assert siliconflow_poll == 21600, f"siliconflow 应 6h (21600), 实际 {siliconflow_poll}"
    assert openrouter_poll == 21600, f"openrouter 应 6h (21600), 实际 {openrouter_poll}"

    # qianwenai 比另两个慢 (4 倍以上间隔)
    assert qianwenai_poll >= 4 * siliconflow_poll, (
        "qianwenai 应与 siliconflow 错峰, 频次 ≥ 4 倍"
    )

    print(f"  [OK] 错峰: siliconflow={siliconflow_poll}s, "
          f"openrouter={openrouter_poll}s, qianwenai={qianwenai_poll}s")


# ============================================================
# Main
# ============================================================
def main():
    print("=" * 60)
    print("Phase 4 v4.2 千问AI 聚合平台情报源 — 单元测试 (6 个)")
    print("=" * 60)

    test_qianwenai_source_basic_attrs()
    test_qianwenai_url_full_utm()
    test_qianwenai_registered_in_get_all_sources()
    test_qianwenai_classified_as_price_aggregator()
    test_get_source_by_provider_qianwenai()
    test_qianwenai_poll_interval_off_peak()

    print("\n" + "=" * 60)
    print("ALL 6 TESTS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    main()
