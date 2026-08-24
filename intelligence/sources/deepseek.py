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
            poll_interval=1800,  # 30分钟 (V4.8.3: 原 15min→30min)
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
            poll_interval=3600,  # 1小时 (V4.8.3: 原 30min→1h)
            css_selector="main",
            # Phase 2B-3: 公告页可能 JS 加载，等主内容容器出现
            wait_until="domcontentloaded",
            wait_for="main",
        )


class DeepSeekModelListSource(IntelSourceBase):
    """DeepSeek model 列表 (Phase 3.6, 2026-08-06)

    用途: 发现 DeepSeek 自家模型 (deepseek-chat / deepseek-coder / deepseek-reasoner)
    API: https://api.deepseek.com/models (OpenAI 兼容)
    频率: 1 天 (DEFAULT_POLL_INTERVALS[MODEL_LIST])

    V4.8.6: base URL 改为 https://api.deepseek.com (不带 /v1).
    DeepSeek 文档明确获取模型列表是 GET /models (无 /v1 前缀).
    fetch_models_via_api 会自动补 /models 后缀 → https://api.deepseek.com/models
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
            # V4.8.6: DeepSeek 文档 base_url 是 https://api.deepseek.com (无 /v1)
            # /v1/models 和 /models 均可访问, 但文档路径是 /models
            # fetch_models_via_api 会自动补 /models 后缀
            api_endpoint="https://api.deepseek.com",
            api_key_from="deepseek",  # 需 channels 表有 deepseek active channel
        )