"""Phase 4 v4.3 (2026-08-13) 价格采集源单元测试

覆盖:
- ModelPricingItem schema (normal/cache_hit/free)
- normalize_model_item 清洗 pricing 字段 (含无效 type / 价格)
- 5 个 source 类实例化 (provider/url/source_type/poll_interval)
- pricing_writer 在无 channel 时跳过 (不抛错)

跑法:
    .venv/Scripts/python.exe tests/test_pricing_sources.py
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _install_mock_config():
    """mock config.settings + openai 模块让 parser.py / pricing_writer.py 能 import"""
    import sys
    import types

    # mock openai (避免需要安装 openai 依赖)
    if "openai" not in sys.modules:
        mock_openai = types.ModuleType("openai")
        mock_openai.AsyncOpenAI = lambda *a, **kw: None
        sys.modules["openai"] = mock_openai

    class _MockSettings:
        openai_api_key = "sk-test"
        openai_base_url = "https://mock.local/v1"
        parse_model = "gpt-4o-mini"
        parse_confidence_threshold = 0.6
        aimc_master_key = "0" * 64
        crawl_enabled = True
        route_refresh_interval = 600
        db_url = "sqlite+aiosqlite:///:memory:"
        db_path = "./data/aimc.db"
        auth_enabled = False
        api_key = ""
        max_fallback_retries = 2
        proxy_url = ""
        alert_channel = "log"
        bark_key = ""
        bark_server = ""
        serverchan_key = ""
        feishu_webhook_url = ""

    sys.modules.setdefault("config", types.ModuleType("config"))
    sys.modules["config"].settings = _MockSettings()


_install_mock_config()


# ============================================================
# Test 1: ModelPricingItem schema
# ============================================================
def test_model_pricing_item_schema():
    """ModelPricingItem 字段默认值 + type 白名单"""
    from schemas.intel import ModelPricingItem, ModelIntelItem
    print("\n=== Test 1: ModelPricingItem schema ===")

    # 默认值
    p = ModelPricingItem()
    assert p.type == "normal", f"默认 type 应 normal, 实际 {p.type}"
    assert p.input_price is None
    assert p.output_price is None
    assert p.time_range == "全天", f"默认 time_range 应全天, 实际 {p.time_range}"

    # cache_hit + free
    p2 = ModelPricingItem(type="cache_hit", input_price=0.0, output_price=0.0)
    assert p2.type == "cache_hit"
    p3 = ModelPricingItem(type="free", input_price=0.0, output_price=0.0)
    assert p3.type == "free"

    # 嵌入到 ModelIntelItem
    item = ModelIntelItem(
        model_id="test-model",
        pricing=[p, p2, p3],
    )
    assert len(item.pricing) == 3
    print("  [OK] ModelPricingItem 字段默认值 + 3 种 type 都正确")


# ============================================================
# Test 2: normalize_model_item 清洗 pricing
# ============================================================
def test_normalize_pricing_field():
    """AI 解析的 pricing 字段清洗 (含无效 type / 价格)"""
    from intelligence.parser import normalize_model_item
    print("\n=== Test 2: normalize_model_item 清洗 pricing ===")

    # 输入包含 4 种 pricing 项 (3 有效 + 1 无效 type)
    raw = {
        "model_id": "test-model",
        "family": "deepseek",
        "confidence": 0.9,
        "pricing": [
            {"type": "normal", "input_price": 6.0, "output_price": 12.0, "time_range": "全天"},
            {"type": "cache_hit", "input_price": 0.0, "output_price": 0.0, "time_range": "全天"},
            {"type": "free", "input_price": 0, "output_price": 0},
            {"type": "INVALID_TYPE", "input_price": 1.0, "output_price": 2.0},  # 无效
        ],
    }
    item = normalize_model_item(raw)
    assert len(item.pricing) == 4, f"4 项 (含 1 无效 type 也保留, 降级为 normal), 实际 {len(item.pricing)}"
    # 最后一项降级为 normal
    assert item.pricing[3].type == "normal", f"无效 type 应降级 normal, 实际 {item.pricing[3].type}"
    # free 项 input_price=0 字符串转 float
    assert item.pricing[2].input_price == 0.0
    print("  [OK] 无效 type 降级 normal + 数字字符串转 float")


# ============================================================
# Test 3: 5 个 source 类实例化
# ============================================================
def test_pricing_sources_init():
    """5 个新 source 类都正确实例化, 参数符合预期"""
    from intelligence.sources.price_aggregator import (
        DeepSeekPricingSource,
        KimiPricingSource,
        MiniMaxPricingSource,
        GLMPricingSource,
        SiliconFlowPricingCenterSource,
        SenseNovaPricingSource,
        StepFunPricingSource,
    )
    from intelligence.sources.base import SourceType
    print("\n=== Test 3: 5 个 pricing source 实例化 ===")

    cases = [
        ("deepseek", DeepSeekPricingSource, "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/", 21600),
        ("kimi", KimiPricingSource, "https://platform.kimi.com/", 3600),
        ("minimax", MiniMaxPricingSource, "https://platform.minimaxi.com/subscribe/token-plan", 3600),
        ("glm", GLMPricingSource, "https://bigmodel.cn/glm-coding", 3600),
        ("sensenova", SenseNovaPricingSource, "https://www.sensenova.cn/token-plan", 3600),
        ("stepfun", StepFunPricingSource, "https://platform.stepfun.com/step-plan", 3600),
        ("siliconflow", SiliconFlowPricingCenterSource, "https://www.siliconflow.cn/pricing", 21600),
    ]

    for expected_provider, cls, expected_url_part, expected_poll in cases:
        s = cls()
        assert s.provider == expected_provider, f"{cls.__name__}: provider 应 {expected_provider}, 实际 {s.provider}"
        assert expected_url_part in s.url, f"{cls.__name__}: url 应包含 {expected_url_part}, 实际 {s.url}"
        assert s.source_type == SourceType.PRICE_AGGREGATOR, \
            f"{cls.__name__}: source_type 应 PRICE_AGGREGATOR, 实际 {s.source_type}"
        assert s.poll_interval == expected_poll, \
            f"{cls.__name__}: poll_interval 应 {expected_poll}, 实际 {s.poll_interval}"

    print(f"  [OK] 5 个 source 实例化全部正确")


# ============================================================
# Test 4: pricing_writer 在无 channel 时跳过
# ============================================================
def test_pricing_writer_skip_without_channel():
    """provider 没有 active channel 时, pricing_writer 优雅跳过不抛错"""
    from intelligence.pricing_writer import write_pricing_snapshots_from_parse
    from schemas.intel import ModelIntelItem, ModelPricingItem, ParseResult
    print("\n=== Test 4: pricing_writer 无 channel 时跳过 ===")

    # mock DB: get_channel_by_provider 返回 None
    import db.crud as crud

    async def _get_channel_none(session, provider, status="active"):
        return None

    import db.engine
    import sqlalchemy.ext.asyncio

    class _MockSession:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False

    crud.get_channel_by_provider = _get_channel_none
    db.engine.async_session_factory = lambda: _MockSession()

    parsed = ParseResult(
        is_promotion=False,
        confidence=0.9,
        title=None,
        provider="nonexistent",
        intelligence_kind="pricing",
        models=[
            ModelIntelItem(
                model_id="test-model",
                pricing=[ModelPricingItem(type="normal", input_price=1.0, output_price=2.0)],
            ),
        ],
    )

    synced, refreshed = asyncio_run_safe(
            write_pricing_snapshots_from_parse(parsed, "nonexistent")
        )
    assert synced == 0 and refreshed == 0, \
        f"无 channel 应 synced=0/refreshed=0, 实际 synced={synced}/refreshed={refreshed}"
    print(f"  [OK] 无 channel 时 synced=0, refreshed=0 (不抛错)")


def asyncio_run_safe(coro):
    """直接跑一个 coroutine 对象, 简单 wrapper"""
    import asyncio
    return asyncio.run(coro)


# ============================================================
# Test 5: get_all_sources 包含 5 个新 source
# ============================================================
def test_get_all_sources_includes_new():
    """scheduler 注册表应含 5 个新 pricing source"""
    from intelligence.sources import get_all_sources
    print("\n=== Test 5: get_all_sources 包含 5 个新 source ===")

    sources = get_all_sources()
    pricing_sources = [s for s in sources if s.url in (
        "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
        "https://platform.kimi.com/",
        "https://platform.minimaxi.com/subscribe/token-plan",
        "https://bigmodel.cn/glm-coding",
        "https://www.siliconflow.cn/pricing",
    )]
    assert len(pricing_sources) == 5, \
        f"应 5 个新 pricing source, 实际 {len(pricing_sources)}"
    print(f"  [OK] get_all_sources() 含 5 个新 pricing source (总 {len(sources)} 个 source)")


# ============================================================
# Main
# ============================================================
def main():
    print("=" * 60)
    print("Phase 4 v4.3 (2026-08-13) 价格采集源单元测试")
    print("=" * 60)
    print("覆盖 5 个: schema / 清洗 / source 实例化 / writer 跳过 / 注册")

    test_model_pricing_item_schema()
    test_normalize_pricing_field()
    test_pricing_sources_init()
    test_pricing_writer_skip_without_channel()
    test_get_all_sources_includes_new()

    print("\n" + "=" * 60)
    print("ALL 5 TESTS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    main()