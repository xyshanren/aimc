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
            poll_interval=43200,  # 12 小时 (V4.8.3: 原 6h→12h)
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
            poll_interval=43200,  # 12 小时 (V4.8.3: 原 6h→12h)
            css_selector="main, body",
            wait_until="networkidle",  # SPA 多, 等网络空闲
            wait_for="main",
            wait_for_timeout=15000,
        )


# ============================================================
# Phase 4 v4.3 (2026-08-13): 5 家厂商官方定价页 / 订阅 plan 页采集源
#
# 设计说明:
# - 复用现有 PRICE_AGGREGATOR 枚举 (source_type)
# - intelligence_kind="pricing" → 写 price_snapshots (含 cache_hit / free)
# - SPA 页面 (Kimi/MiniMax/GLM) 用 networkidle + Playwright stealth
# - 静态页 (DeepSeek/SiliconFlow) 用 domcontentloaded (快)
# ============================================================


class DeepSeekPricingSource(IntelSourceBase):
    """DeepSeek 官方定价页 (静态 HTML, 无 SPA)"""

    def __init__(self):
        super().__init__(
            provider="deepseek",
            url="https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
            source_type=SourceType.PRICE_AGGREGATOR,
            poll_interval=43200,  # 12 小时 (V4.8.3: 原 6h→12h)
            css_selector="table, main, article",
            wait_until="domcontentloaded",
        )


class KimiPricingSource(IntelSourceBase):
    """Kimi/Moonshot 平台首页 (SPA, 含 model 价格 + 充值优惠)"""

    def __init__(self):
        super().__init__(
            provider="kimi",
            url="https://platform.kimi.com/",
            source_type=SourceType.PRICE_AGGREGATOR,
            poll_interval=3600,  # 1 小时 (优惠更新频繁)
            css_selector="main, .pricing-section, .promotion-banner",
            wait_until="networkidle",  # SPA
            wait_for="main",
            wait_for_timeout=20000,
        )


class MiniMaxPricingSource(IntelSourceBase):
    """MiniMax token-plan 页 (SPA, 订阅 + 按量价)

    注意: provider 跟 manual_import.py 一致 (小写 "minimax"), 不是 family 名 "MiniMax".
    pricing_writer 按 provider 查 channel.
    """

    def __init__(self):
        super().__init__(
            provider="minimax",
            url="https://platform.minimaxi.com/subscribe/token-plan",
            source_type=SourceType.PRICE_AGGREGATOR,
            poll_interval=3600,  # 1 小时
            css_selector="main, .pricing, .plan",
            wait_until="networkidle",
            wait_for="main",
            wait_for_timeout=20000,
        )


class GLMPricingSource(IntelSourceBase):
    """智谱 GLM coding plan 页 (SPA, 含 coding 订阅优惠)"""

    def __init__(self):
        super().__init__(
            provider="glm",
            url="https://bigmodel.cn/glm-coding",
            source_type=SourceType.PRICE_AGGREGATOR,
            poll_interval=3600,  # 1 小时
            css_selector="main, .plan-section, .pricing",
            wait_until="networkidle",
            wait_for="main",
            wait_for_timeout=20000,
        )


class SiliconFlowPricingCenterSource(IntelSourceBase):
    """硅基流动定价中心 (含 free / cache_hit / normal 三种价格)

    与现有 SiliconFlowPricingSource (docs.siliconflow.cn/cn-userguide/pricing 文档站) 不同:
    - 文档站: Phase 3.1 时接入, 只列基础 model 名 + 价格
    - 定价中心: 完整价格表, 含 cache_hit (上下文缓存命中免费) 和 free (完全免费) 标识
    """

    def __init__(self):
        super().__init__(
            provider="siliconflow",
            url="https://www.siliconflow.cn/pricing",
            source_type=SourceType.PRICE_AGGREGATOR,
            poll_interval=43200,  # 12 小时 (V4.8.3: 原 6h→12h)
            css_selector="main, .pricing-table, table",
            wait_until="domcontentloaded",
        )


# ============================================================
# Phase 4 v4.3 (2026-08-13): 后续补充的 2 个 token-plan / step-plan 源
# ============================================================


class SenseNovaPricingSource(IntelSourceBase):
    """商汤日日新 SenseNova token-plan 页 (静态 HTML + 含 "Free 公测" 标识)

    页内含:
    - SenseNova 6.8 Flash Lite / SenseNova U1 Fast 模型
    - "Free · 公测" 限时 0 元/月, 每模型 1500 次/5 小时
    - Lite / Pro 即将上线付费档位

    频率: 1 小时 (优惠变更较频繁)
    """

    def __init__(self):
        super().__init__(
            provider="sensenova",
            url="https://www.sensenova.cn/token-plan",
            source_type=SourceType.PRICE_AGGREGATOR,
            poll_interval=3600,  # 1 小时
            css_selector="main, .plan-card, .pricing-section, table",
            wait_until="domcontentloaded",
        )


class StepFunPricingSource(IntelSourceBase):
    """阶跃星辰 StepFun step-plan 页 (Next.js SPA + 订阅套餐)

    页内含:
    - 4 个订阅套餐: Flash Mini (400M credits) / Plus (1600M) / Pro (8000M) / Max (40000M)
    - 促销信息: "月付季付年付最高立省¥1,722"
    - "支持所有旗舰模型和智能路由" (具体名需要从首页 /docs/zh/llm/modeloverview 取)

    频率: 1 小时 (SPA, 优惠更新频繁)
    """

    def __init__(self):
        super().__init__(
            provider="stepfun",
            url="https://platform.stepfun.com/step-plan",
            source_type=SourceType.PRICE_AGGREGATOR,
            poll_interval=3600,  # 1 小时
            css_selector="main, .plan-section, .pricing",
            wait_until="networkidle",  # Next.js SPA
            wait_for="main",
            wait_for_timeout=20000,
        )