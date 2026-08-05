"""Phase 3.1 (2026-07-29) — 采集源扩展 单元测试

覆盖:
1. SourceType 常量正确
2. DEFAULT_POLL_INTERVALS 6 个类型全有
3. get_all_sources 注册 12 个源 (6 旧 + 6 新)
4. get_sources_by_type 按 type 正确分组
5. _sanity_check 按 source_type 调整阈值
6. 各源 poll_interval 符合推荐频率

跑法: python tests/test_phase3_1_sources.py
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ============================================================
# Test 1: SourceType 常量
# ============================================================
def test_sourcetype_constants():
    """SourceType 6 个常量 + DEFAULT_POLL_INTERVALS 全覆盖"""
    print("\n=== Test 1: SourceType 常量 ===")
    from intelligence.sources.base import SourceType, DEFAULT_POLL_INTERVALS

    expected_types = {
        "web_page", "api_doc", "announcement",
        "model_list", "price_aggregator", "benchmark",
    }
    actual_types = set(SourceType.all())
    assert actual_types == expected_types, \
        f"SourceType 应 = {expected_types}, 实际 = {actual_types}"

    # DEFAULT_POLL_INTERVALS 应覆盖 6 个 type
    for t in expected_types:
        assert t in DEFAULT_POLL_INTERVALS, f"DEFAULT_POLL_INTERVALS 缺 {t}"
        interval = DEFAULT_POLL_INTERVALS[t]
        assert interval > 0, f"{t} interval 应 > 0"

    print(f"  [OK] SourceType 常量: {sorted(SourceType.all())}")
    print(f"  [OK] DEFAULT_POLL_INTERVALS: {DEFAULT_POLL_INTERVALS}")


# ============================================================
# Test 2: 源注册总数 (6 旧 + 6 新 = 12)
# ============================================================
def test_source_registration():
    """get_all_sources 应返回 12 个源 (Phase 2A 6 + Phase 3.1 6)"""
    print("\n=== Test 2: 源注册总数 ===")
    from intelligence.sources import get_all_sources

    all_sources = get_all_sources()
    assert len(all_sources) == 12, f"应 12 个源, 实际 {len(all_sources)}"

    # 按 type 分组
    type_counts = {}
    for s in all_sources:
        type_counts[s.source_type] = type_counts.get(s.source_type, 0) + 1
    expected_counts = {
        "web_page": 4,        # deepseek/qwen/glm + qwen activity
        "announcement": 2,     # deepseek/glm
        "model_list": 2,       # siliconflow/volcengine
        "price_aggregator": 2, # siliconflow/openrouter
        "benchmark": 2,        # lmsys/superclue
    }
    assert type_counts == expected_counts, \
        f"type 分布应 = {expected_counts}, 实际 = {type_counts}"

    print(f"  [OK] 总 12 个源: {dict(sorted(type_counts.items()))}")


# ============================================================
# Test 3: get_sources_by_type 按 type 分组
# ============================================================
def test_get_sources_by_type():
    """get_sources_by_type 正确分组"""
    print("\n=== Test 3: get_sources_by_type ===")
    from intelligence.sources import get_sources_by_type

    # model_list 应有 2 个 (siliconflow + volcengine)
    sources = get_sources_by_type("model_list")
    providers = {s.provider for s in sources}
    assert providers == {"siliconflow", "volcengine"}, \
        f"model_list 应含 siliconflow+volcengine, 实际 {providers}"

    # benchmark 应有 2 个
    sources = get_sources_by_type("benchmark")
    providers = {s.provider for s in sources}
    assert providers == {"lmsys", "superclue"}, \
        f"benchmark 应含 lmsys+superclue, 实际 {providers}"

    # 不存在的 type 返回空
    sources = get_sources_by_type("nonexistent")
    assert sources == [], f"不存在的 type 应返 [], 实际 {sources}"

    print(f"  [OK] model_list: siliconflow + volcengine")
    print(f"  [OK] benchmark: lmsys + superclue")
    print(f"  [OK] nonexistent: [] (空)")


# ============================================================
# Test 4: 各源 poll_interval 符合推荐
# ============================================================
def test_poll_intervals():
    """所有 source.poll_interval 应等于或长于 DEFAULT_POLL_INTERVALS[type]"""
    print("\n=== Test 4: poll_interval 校验 ===")
    from intelligence.sources import get_all_sources
    from intelligence.sources.base import DEFAULT_POLL_INTERVALS

    for s in get_all_sources():
        recommended = DEFAULT_POLL_INTERVALS.get(s.source_type, 900)
        assert s.poll_interval >= recommended, \
            f"{s.provider}/{s.source_type}: poll_interval={s.poll_interval} < 推荐 {recommended}"
        # 特定类型必检
        if s.source_type == "model_list":
            assert s.poll_interval >= 86400, \
                f"model_list 至少 1 天, 实际 {s.poll_interval}"
        elif s.source_type == "benchmark":
            assert s.poll_interval >= 604800, \
                f"benchmark 至少 1 周, 实际 {s.poll_interval}"

    print(f"  [OK] 所有 source.poll_interval 符合推荐频率")


# ============================================================
# Test 5: _sanity_check 按 source_type 调整阈值
# ============================================================
def test_sanity_check_by_type():
    """_sanity_check 按 source_type 调整阈值 (Phase 3.1)"""
    print("\n=== Test 5: _sanity_check 按 source_type ===")
    # Mock config (healer 引用)
    import types
    class _MockSettings:
        parse_confidence_threshold = 0.6
    sys.modules['config'] = types.ModuleType('config')
    sys.modules['config'].settings = _MockSettings()

    from intelligence.healer import _sanity_check
    from schemas.intel import ParseResult

    # 5a) web_page conf=0.5 (< 0.6) 标黄
    r = ParseResult(is_promotion=True, confidence=0.5, title="Test title", provider="t")
    flagged, reason = _sanity_check(r, "web_page")
    assert flagged and "low_confidence" in reason, \
        f"web_page conf=0.5 应标黄, 实际 {flagged}/{reason}"

    # 5b) model_list conf=0.5 (>= 0.4) 不标黄
    flagged, reason = _sanity_check(r, "model_list")
    assert not flagged, f"model_list 阈值 0.4, conf=0.5 应通过, 实际 flagged={flagged}"

    # 5c) benchmark conf=0.3 (< 0.4) 标黄
    r2 = ParseResult(is_promotion=True, confidence=0.3, title="Test title", provider="t")
    flagged, reason = _sanity_check(r2, "benchmark")
    assert flagged, f"benchmark conf=0.3 应标黄, 实际 flagged={flagged}"

    # 5d) price_aggregator conf=0.45 (> 0.4) 不标黄
    r3 = ParseResult(is_promotion=True, confidence=0.45, title="Test title", provider="t")
    flagged, reason = _sanity_check(r3, "price_aggregator")
    assert not flagged, f"price_aggregator conf=0.45 应通过, 实际 flagged={flagged}"

    # 5e) 缺省 source_type='web_page' 仍用旧阈值
    r4 = ParseResult(is_promotion=True, confidence=0.5, title="Test title", provider="t")
    flagged, reason = _sanity_check(r4)  # 默认 'web_page'
    assert flagged, f"缺省 source_type=web_page conf=0.5 应标黄, 实际 flagged={flagged}"

    print(f"  [OK] 5 子场景全过 (web_page/model_list/benchmark/price_aggregator/缺省)")


# ============================================================
# Main
# ============================================================

def main():
    print("=" * 60)
    print("Phase 3.1 — 采集源扩展 测试")
    print("=" * 60)

    test_sourcetype_constants()
    test_source_registration()
    test_get_sources_by_type()
    test_poll_intervals()
    test_sanity_check_by_type()

    print("\n" + "=" * 60)
    print("ALL 5 TESTS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    main()