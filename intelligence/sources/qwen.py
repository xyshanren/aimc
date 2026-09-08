"""Qwen (阿里云百炼) 采集源

Phase 3.6 (2026-08-06): 加 QwenModelListSource (百炼 OpenAI 兼容 /v1/models API)
"""

from intelligence.sources.base import IntelSourceBase


class QwenSource(IntelSourceBase):
    """阿里云百炼定价页采集

    2026-09-09: 旧 billing-overview 页已迁移 (200 但 404 壳), 新落点 model-pricing
    (302 实测 + Playwright 渲染 main 10 万字符, content_guard 健康)。
    selector 改 main: 新页 article 只有 55 字符面包屑。
    """

    def __init__(self):
        super().__init__(
            provider="qwen",
            url="https://help.aliyun.com/zh/model-studio/model-pricing",
            source_type="web_page",
            poll_interval=1800,  # 30分钟 (V4.8.3: 原 15min→30min)
            css_selector="main",
            # 阿里云帮助中心 SSR + Playwright 渲染
            wait_until="domcontentloaded",
            wait_for=None,
        )


class QwenActivitySource(IntelSourceBase):
    """阿里云百炼模型清单页采集

    2026-09-09: 原 aliyun.com/activity/intelligent 活动页已 404 下线, 改挂
    help.aliyun.com/zh/model-studio/models 模型清单页 — qwen 无渠道、API 型
    model_list 源走不了, 用 HTML 页补模型发现缺口。
    """

    def __init__(self):
        super().__init__(
            provider="qwen",
            url="https://help.aliyun.com/zh/model-studio/models",
            source_type="web_page",
            poll_interval=3600,  # 1小时 (V4.8.3: 原 30min→1h)
            css_selector="main",
            wait_until="domcontentloaded",
            wait_for=None,
        )


class QwenAnnouncementSource(IntelSourceBase):
    """阿里云百炼 announcement 公告页采集 (Phase 4 v4.3.1, 2026-08-13)

    2026-09-09: 旧 developer-reference/announcement 页已迁移 (200 但 404 壳),
    新落点 release-notes 产品动态页 (SSR + 渲染后 main 含动态标题列表)。
    列表为标题形态, 正文在详情页 — 适合变更检测, 深挖依赖后续按需扩展。

    注: 现有 QwenActivitySource 已改挂 models 页 (web_page 类型),
        本 source 专门针对 announcement/产品动态 (新模型/新功能发布)
    """

    def __init__(self):
        super().__init__(
            provider="qwen",
            url="https://help.aliyun.com/zh/model-studio/release-notes",
            source_type="announcement",
            poll_interval=3600,  # 1小时 (V4.8.3: 原 30min→1h)
            css_selector="main",
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