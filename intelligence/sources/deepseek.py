"""DeepSeek 采集源

Phase 3.6 (2026-08-06): 加 DeepSeekModelListSource (OpenAI 兼容 /v1/models API)
"""

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


class DeepSeekModelListSource(IntelSourceBase):
    """DeepSeek model 列表 (Phase 3.6, 2026-08-06)

    用途: 发现 DeepSeek 自家模型 (deepseek-chat / deepseek-coder / deepseek-reasoner)
    API: https://api.deepseek.com/v1/models (OpenAI 兼容)
    频率: 1 天 (DEFAULT_POLL_INTERVALS[MODEL_LIST])
    """

    def __init__(self):
        super().__init__(
            provider="deepseek",
            url="",  # 无 HTML 文档页 fallback (DeepSeek API-first)
            source_type="model_list",
            poll_interval=86400,
            css_selector=None,
            wait_until="domcontentloaded",
            wait_for=None,
            api_endpoint="https://api.deepseek.com/v1",
            api_key_from="deepseek",  # 需 channels 表有 deepseek active channel
        )