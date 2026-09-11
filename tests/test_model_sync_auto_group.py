"""Phase 4 v4.3.1 (2026-08-13) model_sync auto_group 单元测试

覆盖:
- _find_target_group: T0 永远 tier:flagship
- _find_target_group + AUTO_JOIN_NON_T0=True: T1/T2/T3 自动入组, T4 不入
- _find_target_group + AUTO_JOIN_NON_T0=False (默认): T1/T2/T3/T4 都不入
- should_auto_join / should_auto_leave 阈值不变
- _TIER_TO_GROUP 映射完整性

跑法:
    .venv/Scripts/python.exe tests/test_model_sync_auto_group.py
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def test_tier_to_group_mapping():
    """tier -> group 映射完整性"""
    from intelligence.model_sync import _TIER_TO_GROUP

    # 4 个 tier 都映射
    assert _TIER_TO_GROUP["T0"] == "tier:flagship"
    assert _TIER_TO_GROUP["T1"] == "tier:strong"
    assert _TIER_TO_GROUP["T2"] == "tier:balanced"
    assert _TIER_TO_GROUP["T3"] == "tier:light"
    # T4 不在映射 (需人工)
    assert "T4" not in _TIER_TO_GROUP
    print("  [OK] _TIER_TO_GROUP 映射完整 (T0-T3, T4 排除)")


def test_find_target_group_T0_always():
    """T0 永远返回 tier:flagship (不论 AUTO_JOIN_NON_T0)"""
    import asyncio
    from intelligence.model_sync import _find_target_group, AUTO_JOIN_NON_T0

    original = AUTO_JOIN_NON_T0
    try:
        # AUTO_JOIN_NON_T0=False
        from intelligence import model_sync as ms
        ms.AUTO_JOIN_NON_T0 = False
        result = asyncio.run(_find_target_group(None, "T0"))
        assert result == "tier:flagship"

        # AUTO_JOIN_NON_T0=True
        ms.AUTO_JOIN_NON_T0 = True
        result = asyncio.run(_find_target_group(None, "T0"))
        assert result == "tier:flagship"
        print("  [OK] T0 永远 -> tier:flagship (跟 AUTO_JOIN_NON_T0 无关)")
    finally:
        from intelligence import model_sync as ms
        ms.AUTO_JOIN_NON_T0 = original


def test_find_target_group_default_off():
    """默认 AUTO_JOIN_NON_T0=False: T1/T2/T3/T4 都返回 None"""
    import asyncio
    from intelligence import model_sync as ms

    original = ms.AUTO_JOIN_NON_T0
    try:
        ms.AUTO_JOIN_NON_T0 = False  # 显式 reset
        for tier in ["T1", "T2", "T3", "T4", "T9", "TO"]:
            result = asyncio.run(ms._find_target_group(None, tier))
            assert result is None, f"{tier} 默认应 None, 实际 {result}"
        print("  [OK] AUTO_JOIN_NON_T0=False: T1/T2/T3/T4 都返回 None (跟现有 P0-3 兼容)")
    finally:
        ms.AUTO_JOIN_NON_T0 = original


def test_find_target_group_extended():
    """AUTO_JOIN_NON_T0=True: T1/T2/T3 自动入组, T4 仍不入"""
    import asyncio
    from intelligence import model_sync as ms

    original = ms.AUTO_JOIN_NON_T0
    try:
        ms.AUTO_JOIN_NON_T0 = True
        # T1 -> tier:strong
        result = asyncio.run(ms._find_target_group(None, "T1"))
        assert result == "tier:strong", f"T1 应 tier:strong, 实际 {result}"
        # T2 -> tier:balanced
        result = asyncio.run(ms._find_target_group(None, "T2"))
        assert result == "tier:balanced", f"T2 应 tier:balanced, 实际 {result}"
        # T3 -> tier:light
        result = asyncio.run(ms._find_target_group(None, "T3"))
        assert result == "tier:light", f"T3 应 tier:light, 实际 {result}"
        # T4 仍不入 (映射没覆盖)
        result = asyncio.run(ms._find_target_group(None, "T4"))
        assert result is None, f"T4 应 None, 实际 {result}"
        print("  [OK] AUTO_JOIN_NON_T0=True: T1/T2/T3 自动入组, T4 不入")
    finally:
        ms.AUTO_JOIN_NON_T0 = original


def test_should_auto_join_unchanged():
    """验证 should_auto_join 阈值逻辑 (V6.4 S3 起非 T0 另受 0.9 门槛约束)"""
    from schemas.intel import ModelIntelItem
    from intelligence.model_sync import (
        should_auto_join, JOIN_THRESHOLD, AUTO_JOIN_MIN_CONFIDENCE,
    )

    # confidence >= AUTO_JOIN_MIN_CONFIDENCE (0.9) + tier + family → True
    # (V6.4 S3: 非 T0 的有效门槛从 JOIN_THRESHOLD 0.8 收紧到 0.9)
    item = ModelIntelItem(
        model_id="test-model", family="deepseek",
        inferred_tier="T1", confidence=AUTO_JOIN_MIN_CONFIDENCE,
    )
    assert should_auto_join(item) is True

    # JOIN_THRESHOLD <= confidence < AUTO_JOIN_MIN_CONFIDENCE → False (S3 门槛拦下)
    item.confidence = 0.85
    assert should_auto_join(item) is False

    # confidence < threshold → False
    item.confidence = 0.7
    assert should_auto_join(item) is False

    # T0 不受 S3 门槛约束 (恒免人工): conf=0.8 即可入组
    item.confidence = JOIN_THRESHOLD
    item.inferred_tier = "T0"
    assert should_auto_join(item) is True

    # 没 tier → False
    item.confidence = 0.95
    item.inferred_tier = None
    assert should_auto_join(item) is False

    # 没 family → False
    item.inferred_tier = "T1"
    item.family = None
    assert should_auto_join(item) is False
    print("  [OK] should_auto_join 阈值逻辑 (含 V6.4 S3 门槛)")


def test_should_auto_leave_unchanged():
    """验证 should_auto_leave 阈值逻辑未变"""
    from schemas.intel import ModelIntelItem
    from intelligence.model_sync import should_auto_leave, LEAVE_THRESHOLD

    # deprecated + confidence >= 0.9 → True
    item = ModelIntelItem(
        model_id="old-model", family="deepseek",
        inferred_tier="T1", confidence=0.95, status="deprecated",
    )
    assert should_auto_leave(item) is True

    # 非 deprecated → False
    item.status = "active"
    assert should_auto_leave(item) is False

    # confidence 不达标 → False
    item.status = "deprecated"
    item.confidence = 0.7
    assert should_auto_leave(item) is False
    print("  [OK] should_auto_leave 阈值逻辑不变")


# ============================================================
# 主入口
# ============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("Phase 4 v4.3.1 model_sync AI 自动入组测试")
    print("=" * 60)
    test_tier_to_group_mapping()
    test_find_target_group_T0_always()
    test_find_target_group_default_off()
    test_find_target_group_extended()
    test_should_auto_join_unchanged()
    test_should_auto_leave_unchanged()
    print("=" * 60)
    print("ALL TESTS PASSED")
    print("=" * 60)