"""采集源注册表

管理所有厂商采集源插件。
"""

from intelligence.sources.base import IntelSourceBase
from intelligence.sources.deepseek import (
    DeepSeekSource, DeepSeekAnnouncementSource, DeepSeekModelListSource,
)
from intelligence.sources.qwen import (
    QwenSource, QwenActivitySource, QwenModelListSource,
    # Phase 4 v4.3.1 (2026-08-13): Qwen 公告页
    QwenAnnouncementSource,
)
from intelligence.sources.glm import (
    GLMSource, GLMActivitySource, GLMModelListSource,
)
# Phase 3.1 (2026-07-29): 3 个新采集源类型
from intelligence.sources.model_lists import (
    SiliconFlowModelListSource,
    VolcengineArkModelListSource,
    MinimaxModelListSource,
)
# Phase 3.6 (2026-08-06): 4 个新国内厂商 model_list 源
from intelligence.sources.model_lists_extra import (
    StepFunModelListSource,
    SenseNovaModelListSource,
    HunyuanModelListSource,
    MoonshotModelListSource,
)
from intelligence.sources.price_aggregator import (
    SiliconFlowPricingSource,
    OpenRouterPricingSource,
    # Phase 4 v4.3 (2026-08-13): 5 家厂商定价页
    DeepSeekPricingSource,
    KimiPricingSource,
    MiniMaxPricingSource,
    GLMPricingSource,
    SiliconFlowPricingCenterSource,
    # Phase 4 v4.3 (2026-08-13) 后续补充: 2 个 token-plan / step-plan 源
    SenseNovaPricingSource,
    StepFunPricingSource,
)
# Phase 4 v4.2 (2026-08-06): 阿里千问AI 观察性情报源
from intelligence.sources.qianwenai import QianwenAISource
# V6.5+ (2026-09-14): Free-LLM-Collection 免费 API 合集观察源
from intelligence.sources.free_llm_collection import FreeLLMCollectionSource
from intelligence.sources.benchmark import (
    LMSYSArenaBenchmarkSource,
    SuperCLUEBenchmarkSource,
)
# V4.8.3: 注册 3 个已有但未挂载的 benchmark 源 (V4.6.3 写了源类, 未注册到 get_all_sources)
from intelligence.sources.artificial_analysis import ArtificialAnalysisBenchmarkSource
from intelligence.sources.livebench import LiveBenchBenchmarkSource
from intelligence.sources.opencompass import OpenCompassBenchmarkSource
# Phase 4 v4.3.1 (2026-08-13): 7 家 announcement source (优惠情报采集)
from intelligence.sources.kimi import KimiAnnouncementSource
from intelligence.sources.minimax import MiniMaxAnnouncementSource
from intelligence.sources.hunyuan import HunyuanAnnouncementSource
from intelligence.sources.moonshot import MoonshotAnnouncementSource
from intelligence.sources.sensenova_announcement import SenseNovaAnnouncementSource
from intelligence.sources.stepfun_announcement import StepFunAnnouncementSource
# V6.14 (2026-09-27): sensenova 官方文档站"支持的模型"监测源
from intelligence.sources.sensenova_docs import SenseNovaDocsSource


def get_all_sources() -> list[IntelSourceBase]:
    """获取所有已注册的采集源实例

    Phase 3.6 (2026-08-06): 共 18 个 source
    - 6 个 web_page / announcement (Phase 2A: deepseek/qwen/glm 各 2 个)
    - 10 个 model_list (Phase 3.1 + 3.6: 9 个 + V6.14 sensenova_docs 文档站)
    - 2 个 price_aggregator
    - 1 个 benchmark (实际 2 个 LMSYS + SuperCLUE)

    Phase 4 v4.2 (2026-08-06): +1 个观察性聚合源 QianwenAISource → 共 19 个
    - 3 个 price_aggregator (siliconflow / openrouter / qianwenai)

    V4.8.3 (2026-08-21): +3 个 benchmark 源 (artificial_analysis / livebench / opencompass) → 共 35 个
    """
    return [
        # Phase 2A 原有 (价格页/公告页)
        DeepSeekSource(),
        DeepSeekAnnouncementSource(),
        QwenSource(),
        QwenActivitySource(),
        GLMSource(),
        GLMActivitySource(),
        # Phase 3.1 model_list: 2 个 (HTML 优先, API 降级 — Commit 2 改造)
        SiliconFlowModelListSource(),
        VolcengineArkModelListSource(),
        # Phase 3.6 model_list: 7 个新增 (全部 API 结构化抽取)
        DeepSeekModelListSource(),
        QwenModelListSource(),
        GLMModelListSource(),
        StepFunModelListSource(),
        SenseNovaModelListSource(),
        HunyuanModelListSource(),
        MoonshotModelListSource(),
        # 2026-10-15: MiniMax M 系列供给监控 (模型消失检测数据源)
        MinimaxModelListSource(),
        # Phase 3.1 price_aggregator
        SiliconFlowPricingSource(),
        OpenRouterPricingSource(),
        # Phase 4 v4.3 price_aggregator (5 家厂商官方定价页)
        DeepSeekPricingSource(),
        KimiPricingSource(),
        MiniMaxPricingSource(),
        GLMPricingSource(),
        SiliconFlowPricingCenterSource(),
        # Phase 4 v4.3 后续补充 (2 个 token-plan / step-plan 源)
        SenseNovaPricingSource(),
        StepFunPricingSource(),
        # Phase 4 v4.2 price_aggregator (观察)
        QianwenAISource(),
        # V6.5+ (2026-09-14) price_aggregator (观察): Free-LLM-Collection 免费 API 合集
        FreeLLMCollectionSource(),
        # Phase 3.1 benchmark
        LMSYSArenaBenchmarkSource(),
        SuperCLUEBenchmarkSource(),
        # V4.8.3: 注册 3 个已有但未挂载的 benchmark 源
        ArtificialAnalysisBenchmarkSource(),
        LiveBenchBenchmarkSource(),
        OpenCompassBenchmarkSource(),
        # Phase 4 v4.3.1 announcement source (7 家,补齐 9 厂商优惠情报采集)
        KimiAnnouncementSource(),
        MiniMaxAnnouncementSource(),
        QwenAnnouncementSource(),
        HunyuanAnnouncementSource(),
        MoonshotAnnouncementSource(),
        SenseNovaAnnouncementSource(),
        StepFunAnnouncementSource(),
        # V6.14 (2026-09-27): sensenova 文档站"支持的模型"监测 (侧栏模型清单 hash 变化)
        SenseNovaDocsSource(),
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