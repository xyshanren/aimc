"""GLM (智谱AI) 采集源"""

from intelligence.sources.base import IntelSourceBase


class GLMSource(IntelSourceBase):
    """智谱AI 开放平台定价页采集"""

    def __init__(self):
        super().__init__(
            provider="glm",
            url="https://open.bigmodel.cn/pricing",
            source_type="web_page",
            poll_interval=900,
            css_selector="main",
        )


class GLMActivitySource(IntelSourceBase):
    """智谱AI 活动/公告页采集"""

    def __init__(self):
        super().__init__(
            provider="glm",
            url="https://open.bigmodel.cn/news",
            source_type="announcement",
            poll_interval=1800,
            css_selector="main",
        )
