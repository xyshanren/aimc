"""Phase 3.1 (2026-07-29): 第三方聚合平台价格采集源

用途: 监控硅基流动/火山方舟这种"聚合平台"的价格变动
      跟厂商直连 (DeepSeek 官方/Qwen 官方) 价格对比 → 路由最优
来源类型: price_aggregator

Phase 3.4 价格联动会消费这些 raw_data → 更新 price_snapshots
"""

from intelligence.sources.base import IntelSourceBase, SourceType


class SiliconFlowPricingSource(IntelSourceBase):
    """硅基流动定价页

    频率: 6 小时 (DEFAULT_POLL_INTERVALS[PRICE_AGGREGATOR])
    跟 DeepSeek 官方/Qwen 官方 错开, 避免同时 fetch 撞 API 限流
    """

    def __init__(self):
        super().__init__(
            provider="siliconflow",
            url="https://docs.siliconflow.cn/cn-userguide/pricing",
            source_type=SourceType.PRICE_AGGREGATOR,
            poll_interval=21600,  # 6 小时
            css_selector="article, main, body",
            wait_until="domcontentloaded",
            wait_for=None,
        )


class OpenRouterPricingSource(IntelSourceBase):
    """OpenRouter 模型+价格页 (国际化聚合平台, 备用)

    频率: 6 小时
    """

    def __init__(self):
        super().__init__(
            provider="openrouter",
            url="https://openrouter.ai/models",
            source_type=SourceType.PRICE_AGGREGATOR,
            poll_interval=21600,  # 6 小时
            css_selector="main, body",
            wait_until="networkidle",  # SPA 多, 等网络空闲
            wait_for="main",
            wait_for_timeout=15000,
        )