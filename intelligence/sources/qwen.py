"""Qwen (阿里云百炼) 采集源"""

from intelligence.sources.base import IntelSourceBase


class QwenSource(IntelSourceBase):
    """阿里云百炼定价页采集"""

    def __init__(self):
        super().__init__(
            provider="qwen",
            url="https://help.aliyun.com/zh/model-studio/billing-overview",
            source_type="web_page",
            poll_interval=900,
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
            poll_interval=1800,
            css_selector=None,
            # Phase 2B-3: 活动页 SPA 多，等 article 元素出现
            wait_until="domcontentloaded",
            wait_for="article",
            wait_for_timeout=15000,  # 活动页加载慢，给 15s
        )
