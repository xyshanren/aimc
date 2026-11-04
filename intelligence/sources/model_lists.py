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

    2026-11-04 代码级停用 (enabled=False):
    - 本站没有 volcengine 渠道 → api_key_from="volcengine" 解析不到 key →
      每轮静默降级抓 HTML 文档页 (url 存在就不会走"无 key 静默跳过"分支);
    - 文档页只给模型名 → 解析出的 metadata 无 tier/价格/上下文 →
      inferred_tier=None → 全部落 pending, 零可路由价值
      (实证: 11-02 那批 Seedream / Seedance / Seed-Evolving / DeepSeek-V4.1-Flash);
    - Seedream 是生图、Seedance 是视频生成, 本来就不该进 LLM 网关路由。
    保留注册 (不删类/不删实例) 是为了让它继续出现在采集源管理页可解释,
    删掉会变成 DB 孤儿行 — 不可见也不可管理。
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
            # 2026-11-04: 无渠道无 key 的 HTML 文档页源, 产出全是垃圾 pending
            enabled=False,
        )


class MinimaxModelListSource(IntelSourceBase):
    """MiniMax 模型列表 (2026-10-15)

    用途: M3 系列供给监控 — 模型消失检测的数据源之一
    (官方公告 M2.5/M2.7 系列 2026-11-18 正式下线, 届时列表应移除旧名,
     连续两次同步未出现即自动出组建议, 验证 model_absence 端到端链路)

    频率: 1 天
    """

    def __init__(self):
        super().__init__(
            provider="minimax",
            url="",
            source_type=SourceType.MODEL_LIST,
            poll_interval=86400,  # 1 天
            css_selector=None,
            wait_until="domcontentloaded",
            wait_for=None,
            api_endpoint="https://api.minimaxi.com/v1",
            api_key_from="minimax",
        )