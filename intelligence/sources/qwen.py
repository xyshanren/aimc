"""Qwen (阿里云百炼) 采集源

Phase 3.6 (2026-08-06): 加 QwenModelListSource (百炼 OpenAI 兼容 /v1/models API)
"""

from intelligence.sources.base import IntelSourceBase


class QwenSource(IntelSourceBase):
    """阿里云百炼定价页采集"""

    def __init__(self):
        super().__init__(
            provider="qwen",
            url="https://help.aliyun.com/zh/model-studio/billing-overview",
            source_type="web_page",
            poll_interval=1800,  # 30分钟 (V4.8.3: 原 15min→30min)
            css_selector="article",
            # Phase 2B-3: 阿里云帮助中心是 SSR，DOM 解析即可
            wait_until="domcontentloaded",
            wait_for=None,
        )


class QwenActivitySource(IntelSourceBase):
    """阿里云百炼活动页采集"""

    def __init__(self):
        super().__init__(
            provider="qwen",
            url="https://www.aliyun.com/activity/intelligent",
            source_type="web_page",
            poll_interval=3600,  # 1小时 (V4.8.3: 原 30min→1h)
            css_selector=None,
            # Phase 2B-3: 活动页 SPA 多，等 article 元素出现
            wait_until="domcontentloaded",
            wait_for="article",
            wait_for_timeout=15000,  # 活动页加载慢，给 15s
        )


class QwenAnnouncementSource(IntelSourceBase):
    """阿里云百炼 announcement 公告页采集 (Phase 4 v4.3.1, 2026-08-13)

    URL: https://help.aliyun.com/zh/model-studio/developer-reference/announcement
    抓取策略: domcontentloaded (阿里云帮助中心 SSR)
    频率: 30 分钟 (DEFAULT_POLL_INTERVALS[ANNOUNCEMENT])

    注: 现有 QwenActivitySource 是 web_page 类型 (通用活动页),
        本 source 专门针对 announcement (新模型/新功能发布)
    """

    def __init__(self):
        super().__init__(
            provider="qwen",
            url="https://help.aliyun.com/zh/model-studio/developer-reference/announcement",
            source_type="announcement",
            poll_interval=3600,  # 1小时 (V4.8.3: 原 30min→1h)
            css_selector="article",
            wait_until="domcontentloaded",
            wait_for=None,
        )


class QwenModelListSource(IntelSourceBase):
    """阿里云百炼 model 列表 (Phase 3.6, 2026-08-06)

    用途: 发现百炼上的 Qwen 全系模型 (qwen-turbo / qwen-plus / qwen-max / qwen-coder 等)
    API: https://dashscope.aliyuncs.com/compatible-mode/v1/models (OpenAI 兼容)
    频率: 1 天
    """

    def __init__(self):
        super().__init__(
            provider="qwen",
            url="",  # 百炼文档页不易直接抽取, API 优先
            source_type="model_list",
            poll_interval=86400,
            css_selector=None,
            wait_until="domcontentloaded",
            wait_for=None,
            api_endpoint="https://dashscope.aliyuncs.com/compatible-mode/v1",
            api_key_from="qwen",
        )