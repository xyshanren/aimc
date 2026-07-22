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
        )
