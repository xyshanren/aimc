"""采集源注册表

管理所有厂商采集源插件。
"""

from intelligence.sources.base import IntelSourceBase
from intelligence.sources.deepseek import DeepSeekSource, DeepSeekAnnouncementSource
from intelligence.sources.qwen import QwenSource, QwenActivitySource
from intelligence.sources.glm import GLMSource, GLMActivitySource
# Phase 3.1 (2026-07-29): 3 个新采集源类型
from intelligence.sources.model_lists import (
    SiliconFlowModelListSource,
    VolcengineArkModelListSource,
)
from intelligence.sources.price_aggregator import (
    SiliconFlowPricingSource,
    OpenRouterPricingSource,
)
from intelligence.sources.benchmark import (
    LMSYSArenaBenchmarkSource,
    SuperCLUEBenchmarkSource,
)


def get_all_sources() -> list[IntelSourceBase]:
    """获取所有已注册的采集源实例"""
    return [
        # Phase 2A 原有 (价格页/公告页)
        DeepSeekSource(),
        DeepSeekAnnouncementSource(),
        QwenSource(),
        QwenActivitySource(),
        GLMSource(),
        GLMActivitySource(),
        # Phase 3.1 新增 3 个类型 × 2 个源 = 6 个
        SiliconFlowModelListSource(),
        VolcengineArkModelListSource(),
        SiliconFlowPricingSource(),
        OpenRouterPricingSource(),
        LMSYSArenaBenchmarkSource(),
        SuperCLUEBenchmarkSource(),
    ]


def get_source_by_provider(provider: str) -> list[IntelSourceBase]:
    """按厂商获取采集源"""
    return [s for s in get_all_sources() if s.provider == provider]


def get_sources_by_type(source_type: str) -> list[IntelSourceBase]:
    """Phase 3.1: 按类型获取采集源 (调度器按 type 分组用)

    Args:
        source_type: SourceType 常量之一 (e.g. "model_list" / "benchmark")
    """
    return [s for s in get_all_sources() if s.source_type == source_type]
