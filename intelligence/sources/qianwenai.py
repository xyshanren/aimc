"""Phase 4 v4.2 (2026-08-06): 阿里千问AI 聚合平台情报源

背景:
- 千问AI 是阿里系聚合型平台 (https://www.qianwenai.com/),
  聚合多模型 (kimi/deepseek/GLM/Qwen 等), 提供统一调用入口.
- 本采集源定位为 **观察 / 监控** 用途:
  - 不参与路由 (reason= 不能加 <html-script>, /v1/models 无公开)
  - 监控该平台是否值得接入 (上线新模型 / 调整价格 / 营销活动文案 反映聚合层动态)
  - 助力 dashboard "第三方观察" 维度, 跟踪聚合平台生态

设计:
- 来源类型: PRICE_AGGREGATOR (符合"第三方聚合平台"语义)
- URL: https://www.qianwenai.com/?source_channel=hy_qwen&utm_content=g_1000415132
- 频率: 1 天 (聚合平台页变更中等, 跟 siliconflow/openrouter 6h 错开, 避免资源争抢)
- wait_until=networkidle: SPA 渲染, 等网络空闲 (仿 openrouter 处理)
- 不配 api_endpoint: 该平台未公开 OpenAI 兼容 /v1/models (聚合, 而非 API-first 厂商)

数据消费:
- 跟 siliconflow/openrouter pricing 源一样, 走 HTML 抓取 + AI 解析
- 解析结果落 intel_change_log + content_snapshots
- dashboard "第三方观察" section 展示, **不进 model_registry / 不进路由**

潜在远期动作 (本 commit 不做, 仅记录):
- 若聚合 layer 价格稳定 + 模型覆盖广, 可考虑接入作为二级兜底渠道 (待人工评估)
"""

from intelligence.sources.base import IntelSourceBase, SourceType


class QianwenAISource(IntelSourceBase):
    """千问AI 聚合平台 (阿里, 观察性数据源)

    监控目标:
    - 上线的模型清单变动 (kimi / deepseek / GLM / Qwen 各家上新)
    - 聚合价格变动 (对比官方原价 / 其他聚合平台如硅基流动)
    - 营销活动文案变动 (满减 / 限时免费 / 首单优惠)

    频率: 1 天 (86400s, 跟 siliconflow/openrouter 6h 错开)

    调用路径:
    - scheduler._run_source_task → fetcher.fetch_page_with_error(networkidle)
      → AI 解析 → differ.are_pages_different → intel_change_log
      → dashboard "/api/intel/observations" 展示
    """

    def __init__(self):
        super().__init__(
            provider="qianwenai",
            url="https://www.qianwenai.com/?source_channel=hy_qwen&utm_content=g_1000415132",
            source_type=SourceType.PRICE_AGGREGATOR,
            poll_interval=86400,  # 1 天
            css_selector="main, body, .container, .content",
            wait_until="networkidle",  # SPA 渲染
            wait_for="main",
            wait_for_timeout=15000,
            # 千问AI 无公开 /v1/models, 不配 api_endpoint
            # 纯 HTML 抓取 + AI 解析 (scheduler._run_source_task 走 fallback 路径)
            api_endpoint=None,
            api_key_from=None,
        )
