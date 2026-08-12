"""Phase 3.6 (2026-08-06): 新增 4 个国内 LLM 服务商 model_list 源

背景:
- 之前 Phase 3.1 只接了 2 个 model_list 源 (硅基流动 + 火山方舟)
- 国内还有 4 个主流厂商未接: 阶跃星辰 / 商汤日日新 / 腾讯混元 / 月之暗面 Kimi
- 全部走 /v1/models API 结构化抽取 (Commit 2 实现)

设计:
- 全部用 api_endpoint, 不配 HTML url (4 厂商均为 API-first, 无公开 model list HTML)
- scheduler 在 API 失败 + url="" 时直接跳过 + handle_fetch_error (避免刷 playwright 日志)
- api_key_from = provider, 需 channels 表对应 provider 的 active channel
- 9 家厂商覆盖统计见 docs/development-plan.md 3.6 章节

API 鉴权情况 (来自厂商官方文档):
- 阶跃星辰: 必需 (每个 model 限速独立, 匿名无法 list)
- 商汤日日新: 必需 (account tier 决定可见 model)
- 腾讯混元: 必需 (SecretId 鉴权, 走 OpenAI 兼容 Bearer)
- 月之暗面 Kimi: 必需 (按 account 限 model 可见性)

如未配 channel, 鉴权失败 → handle_fetch_error → 3 次连续失败后自动 disable
"""

import os

from intelligence.sources.base import IntelSourceBase, SourceType


class StepFunModelListSource(IntelSourceBase):
    """阶跃星辰 StepFun model 列表

    阶跃星辰 (StepFun) 是国产 AI 公司, 旗舰产品 Step-2 / Step-1.5V (多模态)
    API: https://api.stepfun.com/v1/models (OpenAI 兼容)
    频率: 1 天
    """

    def __init__(self):
        super().__init__(
            provider="stepfun",
            url="",
            source_type=SourceType.MODEL_LIST,
            poll_interval=86400,
            css_selector=None,
            wait_until="domcontentloaded",
            wait_for=None,
            api_endpoint="https://api.stepfun.com/v1",
            api_key_from="stepfun",
        )


class SenseNovaModelListSource(IntelSourceBase):
    """商汤日日新 SenseNova model 列表

    商汤科技 (SenseTime) 的日日新大模型 (SenseNova), 多模态 / 视觉见长
    API: https://token.sensenova.cn/v1/models (OpenAI 兼容, 公测 token plan 入口)
    历史 endpoint: https://api.sensenova.cn/v1/models (部分 model 不识别, 1214 modelCode不存在)
    频率: 1 天
    """

    def __init__(self):
        super().__init__(
            provider="sensenova",
            url="",
            source_type=SourceType.MODEL_LIST,
            poll_interval=86400,
            css_selector=None,
            wait_until="domcontentloaded",
            wait_for=None,
            api_endpoint=os.getenv("SENSENOVA_BASE_URL", "https://token.sensenova.cn/v1"),
            api_key_from="sensenova",
        )


class HunyuanModelListSource(IntelSourceBase):
    """腾讯混元 Hunyuan model 列表

    腾讯混元大模型 (Hunyuan), 混元-pro / 混元-standard / 混元-turbo 等
    API: https://api.hunyuan.cloud.tencent.com/v1/models (OpenAI 兼容)
    频率: 1 天
    """

    def __init__(self):
        super().__init__(
            provider="hunyuan",
            url="",
            source_type=SourceType.MODEL_LIST,
            poll_interval=86400,
            css_selector=None,
            wait_until="domcontentloaded",
            wait_for=None,
            api_endpoint="https://api.hunyuan.cloud.tencent.com/v1",
            api_key_from="hunyuan",
        )


class MoonshotModelListSource(IntelSourceBase):
    """月之暗面 Moonshot/Kimi model 列表

    月之暗面 (Moonshot AI) 的 Kimi 系列模型
    旗舰: moonshot-v1-128k / 32k / 8k + kimi-k2 等
    API: https://api.moonshot.cn/v1/models (OpenAI 兼容)
    频率: 1 天
    """

    def __init__(self):
        super().__init__(
            provider="moonshot",
            url="",
            source_type=SourceType.MODEL_LIST,
            poll_interval=86400,
            css_selector=None,
            wait_until="domcontentloaded",
            wait_for=None,
            api_endpoint="https://api.moonshot.cn/v1",
            api_key_from="moonshot",
        )