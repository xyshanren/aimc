"""Phase 3.1 (2026-07-29): 模型列表页采集源

用途: 自动发现新 model_id, 用于 model_registry 自动入组 (Phase 3.3 消费)
来源类型: model_list (跟 web_page/announcement 平级)

实现策略 (半天工作量最小可用版):
- 抓模型列表页 (硅基流动 + 火山方舟)
- AI 解析抽取 model_id 列表 (Phase 3.2 会升级到更结构化)
- 入库到 promotions 表 (复用现有路径), 用 raw_data JSON 字段存 model 列表
- Phase 3.3 自动入组时读 promotions.raw_data → model_registry
"""

from intelligence.sources.base import IntelSourceBase, SourceType


class SiliconFlowModelListSource(IntelSourceBase):
    """硅基流动模型列表页

    用途: 发现硅基流动支持的 100+ model_id (实际是 Phase 2.5 #1 已经拉的 91 个,
    但这个 source 是兜底: 当硅基流动加新 model 但 refresh_siliconflow_models.py
    还没跑时, 这里能 catch)

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
        )


class VolcengineArkModelListSource(IntelSourceBase):
    """火山引擎方舟模型列表页

    用途: 发现火山方舟的 model (豆包/Doubao-pro 等), 用于扩展渠道覆盖

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
        )