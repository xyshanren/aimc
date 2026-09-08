"""Phase 3.2 (2026-07-29) — AI 解析器升级 单元测试

覆盖：
1. confidence_level 分级 (low/medium/high)
2. infer_tier 显式 tier 优先
3. infer_tier 缺乏依据时返 None
4. infer_tier 按参数规模保守分级 (T0~T4)
5. normalize_model_item 校验/清洗 (capabilities 受限枚举)
6. normalize_model_item confidence 截断到 0~1
7. normalize_model_item 缺 model_id 抛错
8. parse_content 解析带 models 的 JSON
9. parse_content 解析无 models 兼容老数据
10. 异常向上抛（不静默返回空）

跑法：python tests/test_phase3_2_parser.py
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ============================================================
# Mock config (parser 引用 settings.openai_api_key/base_url/parse_model)
# ============================================================
def _install_mock_config():
    import types

    class _MockSettings:
        openai_api_key = "sk-test"
        openai_base_url = "https://mock.local/v1"
        parse_model = "gpt-4o-mini"
        parse_confidence_threshold = 0.6

    sys.modules.setdefault("config", types.ModuleType("config"))
    sys.modules["config"].settings = _MockSettings()


_install_mock_config()


def _install_mock_openai():
    import sys
    import types

    mod = types.ModuleType("openai")

    class _AsyncOpenAI:
        def __init__(self, *args, **kwargs):
            pass

    mod.AsyncOpenAI = _AsyncOpenAI
    sys.modules.setdefault("openai", mod)


_install_mock_openai()


# ============================================================
# Test 1: confidence_level 分级
# ============================================================
def test_confidence_level_buckets():
    print("\n=== Test 1: confidence_level 分级 ===")
    from intelligence.parser import confidence_level

    assert confidence_level(0.95) == "high"
    assert confidence_level(0.8) == "high"
    assert confidence_level(0.79) == "medium"
    assert confidence_level(0.6) == "medium"
    assert confidence_level(0.59) == "low"
    assert confidence_level(0.0) == "low"
    print("  [OK] low/medium/high 三档正确 (阈值 0.6 / 0.8)")


# ============================================================
# Test 2: infer_tier 显式 tier 优先
# ============================================================
def test_infer_tier_explicit_priority():
    print("\n=== Test 2: infer_tier 显式 tier 优先 ===")
    from intelligence.parser import infer_tier

    tier, reason = infer_tier("whatever-1b", "T3")
    assert tier == "T3", f"显式 T3 应原样返回, 实际 {tier}"
    assert "AI" in reason
    print(f"  [OK] 显式 T3 覆盖参数推断, reason={reason!r}")


# ============================================================
# Test 3: infer_tier 显式非法 tier 仍回退到参数推断
# ============================================================
def test_infer_tier_invalid_falls_back_to_size():
    print("\n=== Test 3: 显式非法 tier 回退到参数推断 ===")
    from intelligence.parser import infer_tier

    tier, reason = infer_tier("Qwen-72B-Chat", "T9")
    # 70B → T1 (强力档) per architecture.md §4.2
    assert tier == "T1", f"非法 T9 应回退, 应=T1, 实际 {tier}"
    assert "72" in reason
    print(f"  [OK] 非法 T9 回退到 T1 (强力档), reason={reason!r}")


# ============================================================
# Test 4: infer_tier 缺参数时返 None (防 AI 编造)
# ============================================================
def test_infer_tier_unknown_returns_none():
    print("\n=== Test 4: 缺参数返 None ===")
    from intelligence.parser import infer_tier

    tier, reason = infer_tier("Qwen-Flash", None)
    assert tier is None, f"无法推断应=None, 实际 {tier}"
    assert "缺乏" in reason or "参数" in reason
    print(f"  [OK] Qwen-Flack 无参数返 None, reason={reason!r}")


# ============================================================
# Test 5: infer_tier 按参数规模保守分级
# ============================================================
def test_infer_tier_by_size():
    print("\n=== Test 5: 参数规模保守分级 (T0=旗舰, T4=微型) ===")
    from intelligence.parser import infer_tier

    cases = [
        ("Qwen3-235B-A22B", "T0"),  # 235B → 旗舰
        ("DeepSeek-V3-671B", "T0"),  # 671B → 旗舰
        ("Qwen-72B", "T1"),          # 72B → 强力
        ("Qwen-32B", "T2"),          # 32B → 均衡
        ("Qwen-7B", "T3"),           # 7B → 轻量
        ("bge-small-1.5b", "T4"),    # 1.5B → 微型
        ("Qwen3-1.7B", "T4"),        # 1.7B → 微型
    ]
    for model_id, expected in cases:
        tier, _ = infer_tier(model_id)
        assert tier == expected, f"{model_id} 应={expected}, 实际 {tier}"
        print(f"  [OK] {model_id:30} -> {tier}")


# ============================================================
# Test 6: normalize_model_item 清洗
# ============================================================
def test_normalize_model_item_cleans():
    print("\n=== Test 6: normalize_model_item 清洗 ===")
    from intelligence.parser import normalize_model_item

    raw = {
        "model_id": "DeepSeek-V3-671B",
        "family": "deepseek",
        "display_name": "DeepSeek V3",
        "inferred_tier": "T0",  # 671B 调上参数推断也是 T0, 顺带验证 “显式 tier 与推断一致”
        "capabilities": ["reasoning", "code", "魔改能力", "HACKING"],
        "context_length": "64000",
        "modality": "multimodal",
        "is_open_source": True,
        "status": "active",
        "confidence": 0.92,
    }
    item = normalize_model_item(raw)
    assert item.model_id == "DeepSeek-V3-671B"
    assert item.inferred_tier == "T0"
    assert "reasoning" in item.capabilities
    assert "code" in item.capabilities
    assert "魔改能力" not in item.capabilities  # 受限枚举
    assert "hacking" not in item.capabilities
    assert item.context_length == 64000
    assert item.modality == "multimodal"
    assert item.confidence == 0.92
    assert item.confidence_level == "high"
    assert item.family == "deepseek"
    assert item.is_open_source is True
    print(f"  [OK] cleaned: tier={item.inferred_tier} caps={item.capabilities} "
          f"ctx={item.context_length} level={item.confidence_level}")


# ============================================================
# Test 7: normalize_model_item confidence 截断
# ============================================================
def test_normalize_model_item_confidence_clamp():
    print("\n=== Test 7: confidence 截断到 0~1 ===")
    from intelligence.parser import normalize_model_item

    high = normalize_model_item({"model_id": "m-7b", "confidence": 1.5})
    assert high.confidence == 1.0, f"超 1.0 应截为 1.0, 实际 {high.confidence}"

    neg = normalize_model_item({"model_id": "m-7b", "confidence": -0.3})
    assert neg.confidence == 0.0, f"负值应截为 0.0, 实际 {neg.confidence}"

    bad = normalize_model_item({"model_id": "m-7b", "confidence": "0.55"})
    assert bad.confidence == 0.55

    none = normalize_model_item({"model_id": "m-7b"})
    assert none.confidence == 0.0
    print("  [OK] confidence 越界/非法/None 均安全处理")


# ============================================================
# Test 8: normalize_model_item 缺 model_id 抛错
# ============================================================
def test_normalize_model_item_missing_id():
    print("\n=== Test 8: 缺 model_id 抛错 ===")
    from intelligence.parser import normalize_model_item

    try:
        normalize_model_item({"family": "qwen"})
    except ValueError as exc:
        assert "model_id" in str(exc)
        print(f"  [OK] 抛 ValueError: {exc}")
        return
    raise AssertionError("应抛 ValueError")


# ============================================================
# Test 9: parse_content 解析带 models 的 JSON
# ============================================================
def test_parse_content_with_models(monkeypatch):
    print("\n=== Test 9: parse_content 解析 models ===")
    import asyncio

    from schemas.intel import ParseResult
    from intelligence import parser

    sample = json_dumps = (
        '{"is_promotion": false, "confidence": 0.85, "title": null, '
        '"provider": "siliconflow", "discount_desc": null, "content": null, '
        '"intelligence_kind": "model_metadata", "summary": null, "models": ['
        '  {"model_id": "Qwen/Qwen2.5-72B-Instruct", "family": "qwen", '
        '   "inferred_tier": "T3", "capabilities": ["reasoning", "code"], '
        '   "context_length": 32768, "confidence": 0.9}, '
        '  {"model_id": "Pro/deepseek-ai/DeepSeek-V3", "family": "deepseek", '
        '   "inferred_tier": "T2", "capabilities": ["code"], '
        '   "context_length": 64000, "confidence": 0.85}'
        ']}'
    )

    class _MockChoice:
        message = type("M", (), {"content": sample})()

    class _MockResponse:
        choices = [_MockChoice()]

    class _MockCompletions:
        async def create(self, **kwargs):
            return _MockResponse()

    class _MockChat:
        completions = _MockCompletions()

    class _MockClient:
        chat = _MockChat()

    parser._client = _MockClient()

    result = asyncio.run(
        parser.parse_content("<html>...</html>", "siliconflow", "https://x", "model_list")
    )
    assert isinstance(result, ParseResult)
    assert result.intelligence_kind == "model_metadata"
    assert result.source_type == "model_list"
    assert len(result.models) == 2
    assert result.models[0].model_id == "Qwen/Qwen2.5-72B-Instruct"
    assert result.models[0].inferred_tier == "T3"
    assert result.models[0].confidence_level == "high"
    assert result.models[1].model_id == "Pro/deepseek-ai/DeepSeek-V3"
    print(f"  [OK] 解析到 {len(result.models)} 个 model 元信息")
    for m in result.models:
        print(f"        - {m.model_id}  tier={m.inferred_tier}  "
              f"caps={m.capabilities}  level={m.confidence_level}")


# ============================================================
# Test 10: parse_content 兼容老格式 (无 models)
# ============================================================
def test_parse_content_legacy(monkeypatch):
    print("\n=== Test 10: 兼容老格式 (无 models) ===")
    import asyncio

    from intelligence import parser

    legacy = (
        '{"is_promotion": true, "confidence": 0.8, "title": "8折优惠", '
        '"provider": "deepseek", "discount_desc": "8折", "summary": "限时"}'
    )

    class _MockChoice:
        message = type("M", (), {"content": legacy})()

    class _MockResponse:
        choices = [_MockChoice()]

    class _MockCompletions:
        async def create(self, **kwargs):
            return _MockResponse()

    class _MockChat:
        completions = _MockCompletions()

    class _MockClient:
        chat = _MockChat()

    parser._client = _MockClient()

    result = asyncio.run(
        parser.parse_content("<html/>", "deepseek", "https://x", "web_page")
    )
    assert result.is_promotion is True
    assert result.confidence == 0.8
    assert result.title == "8折优惠"
    assert result.intelligence_kind == "promotion"
    assert result.models == []  # 老格式空数组
    print("  [OK] 老格式 JSON 仍能解析 (models 默认为 [])")


# ============================================================
# Test 11: V6.3.2 — AI 返回自由文本 provider 不覆盖采集源规范值
# 背景: AI 曾返回 "Kimi (Moonshot AI)" / "SenseNova 商汤" 等变体并入库,
#       导致看板按厂商聚合出现 17 种写法
# ============================================================
def test_parse_content_provider_canonical(monkeypatch):
    print("\n=== Test 11: provider 一律取采集源规范值 ===")
    import asyncio

    from intelligence import parser

    # AI 无视规范, 返回自由文本 provider (历史上真实出现的变体)
    rogue = (
        '{"is_promotion": true, "confidence": 0.9, "title": "限时5折", '
        '"provider": "Kimi (Moonshot AI)", "discount_desc": "5折"}'
    )

    class _MockChoice:
        message = type("M", (), {"content": rogue})()

    class _MockResponse:
        choices = [_MockChoice()]

    class _MockCompletions:
        async def create(self, **kwargs):
            return _MockResponse()

    class _MockChat:
        completions = _MockCompletions()

    class _MockClient:
        chat = _MockChat()

    parser._client = _MockClient()

    result = asyncio.run(
        parser.parse_content("<html/>", "kimi", "https://x", "web_page")
    )
    assert result.provider == "kimi", (
        f"AI 自由文本 provider 不应覆盖采集源规范值, got {result.provider!r}"
    )
    # AI 也不再被要求返回 provider 字段 (schema 中已移除)
    assert "provider" not in parser.SYSTEM_PROMPT.split("规则")[0]
    print(f"  [OK] AI 返回 'Kimi (Moonshot AI)' → 入库 provider={result.provider!r}")


# ============================================================
# Main
# ============================================================
def main():
    print("=" * 60)
    print("Phase 3.2 — AI 解析器升级 测试")
    print("=" * 60)

    test_confidence_level_buckets()
    test_infer_tier_explicit_priority()
    test_infer_tier_invalid_falls_back_to_size()
    test_infer_tier_unknown_returns_none()
    test_infer_tier_by_size()
    test_normalize_model_item_cleans()
    test_normalize_model_item_confidence_clamp()
    test_normalize_model_item_missing_id()
    test_parse_content_with_models(monkeypatch=None)
    test_parse_content_legacy(monkeypatch=None)
    test_parse_content_provider_canonical(monkeypatch=None)

    print("\n" + "=" * 60)
    print("ALL 11 TESTS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    main()
