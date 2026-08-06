"""Phase 3.1 (2026-07-29): 模型列表页采集源

用途: 自动发现新 model_id, 用于 model_registry 自动入组 (Phase 3.3 消费)
来源类型: model_list (跟 web_page/announcement 平级)

实现策略演进:
- Phase 3.1 (2026-07-29): HTML 抓取 + AI 解析 (硅基流动 + 火山方舟)
- Phase 3.6 (2026-08-06): 升级为 API 结构化抽取 (基线), HTML 降级保留
  - 优先 api_endpoint 调 /v1/models, 0.5-2s 拿到 100% 准确结果
  - API 鉴权失败 / 5xx → 降级 HTML (原 url)
  - 鉴权 key 从 channels 表读 (Phase 3.6 _resolve_api_key)
- Phase 3.3 自动入组时读 promotions.raw_data → model_registry
"""

from intelligence.sources.base import IntelSourceBase, SourceType


class SiliconFlowModelListSource(IntelSourceBase):
    """硅基流动模型列表

    用途: 发现硅基流动支持的 100+ model_id (实际是 Phase 2.5 #1 已经拉的 91 个,
    但这个 source 是兜底: 当硅基流动加新 model 但 refresh_siliconflow_models.py
    还没跑时, 这里能 catch)

    Phase 3.6: API 结构化抽取 (主) + HTML 文档页 (降级)
    频率: 1 天 (DEFAULT_POLL_INTERVALS[MODEL_LIST])
    """

    def __init__(self):
        super().__init__(
            provider="siliconflow",
            url="https://docs.siliconflow.cn/cn-userguide/quickstart",
            source_type=SourceType.MODEL_LIST,
            poll_interval=86400,  # 1 天
            css_selector="article, main, body",
            wait_until="domcontentloaded",
            wait_for=None,
            # Phase 3.6: API 结构化抽取 (主路径)
            api_endpoint="https://api.siliconflow.cn/v1",
            api_key_from="siliconflow",
        )


class VolcengineArkModelListSource(IntelSourceBase):
    """火山引擎方舟模型列表

    用途: 发现火山方舟的 model (豆包/Doubao-pro 等), 用于扩展渠道覆盖

    Phase 3.6: API 结构化抽取 (主) + HTML 文档页 (降级)
    频率: 1 天
    """

    def __init__(self):
        super().__init__(
            provider="volcengine",
            url="https://www.volcengine.com/docs/82379",
            source_type=SourceType.MODEL_LIST,
            poll_interval=86400,  # 1 天
            css_selector="article, .markdown-body, main",
            wait_until="domcontentloaded",
            wait_for=None,
            # Phase 3.6: API 结构化抽取 (主路径)
            api_endpoint="https://ark.cn-beijing.volces.com/api/v3",
            api_key_from="volcengine",
        )