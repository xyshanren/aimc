"""GLM (智谱AI) 采集源

Phase 3.6 (2026-08-06): 加 GLMModelListSource (智谱 GLM 风格 /v1/models API)
"""

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
            # Phase 2B-3: 定价页静态
            wait_until="domcontentloaded",
            wait_for=None,
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
            # Phase 2B-3: 公告页
            wait_until="domcontentloaded",
            wait_for="main",
        )


class GLMModelListSource(IntelSourceBase):
    """智谱AI model 列表 (Phase 3.6, 2026-08-06)

    用途: 发现智谱 GLM 全系模型 (glm-4 / glm-4-flash / glm-4-plus / cogview 等)
    API: https://open.bigmodel.cn/api/paas/v4/models (智谱 GLM 风格, {data: [{id: ...}]})
    频率: 1 天
    """

    def __init__(self):
        super().__init__(
            provider="glm",
            url="",  # 智谱 API-first, 无公开 model list HTML 页
            source_type="model_list",
            poll_interval=86400,
            css_selector=None,
            wait_until="domcontentloaded",
            wait_for=None,
            api_endpoint="https://open.bigmodel.cn/api/paas/v4",
            api_key_from="glm",
        )