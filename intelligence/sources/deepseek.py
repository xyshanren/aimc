"""DeepSeek 采集源"""


from intelligence.sources.base import IntelSourceBase


class DeepSeekSource(IntelSourceBase):
    """DeepSeek 官方文档站/定价页采集"""

    def __init__(self):
        super().__init__(
            provider="deepseek",
            url="https://api.deepseek.com/pricing",
            source_type="web_page",
            poll_interval=900,  # 15分钟
            css_selector="main",
            # Phase 2B-3: pricing 页是静态 HTML，DOM 解析完即可
            wait_until="domcontentloaded",
            wait_for=None,
        )


class DeepSeekAnnouncementSource(IntelSourceBase):
    """DeepSeek 公告页采集"""

    def __init__(self):
        super().__init__(
            provider="deepseek",
            url="https://api.deepseek.com/news",
            source_type="announcement",
            poll_interval=1800,  # 30分钟
            css_selector="main",
            # Phase 2B-3: 公告页可能 JS 加载，等主内容容器出现
            wait_until="domcontentloaded",
            wait_for="main",
        )
