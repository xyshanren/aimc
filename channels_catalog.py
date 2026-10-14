"""渠道目录 (单一事实源)

被两处消费:
1. scripts/manual_import.py — .env 引导导入 (读 key/name/provider/base_url/
   priority/env_var/qpm/prices, 其余键忽略)
2. dashboard/onboarding.py — 看板「初始化配置」页 (额外读 onboarding 元数据:
   分类/文案/注册链接/控制台链接)

⚠️ 价格行单位 = 元/百万 tokens; 第 6 个元素 = cached_input_price (可选)。
   model 命名: 无 "/" 的短名必须已 normalize_model_id 归一化 (小写);
   聚合平台 raw 名 (含命名空间 e.g. "deepseek-ai/DeepSeek-V4.1-Flash") 原样直录。
"""

import os

# ============================================================
# 模型注册表元数据 (db/seed_default.py 消费, 2026-10-14)
#
# 初始化时按此建 model_registry 行 + 按 tier 自动入默认组:
#   T0→tier:flagship  T1→tier:strong  T2→tier:balanced  T3/T4→tier:light
#   multimodal/vision 额外入 tier:vision (T0/T1) 或 tier:vision-light (T2+)
#   capabilities 含 code 额外入 tier:code
# 生图/暂不入组模型不列在此处 (仅价格行存在, 不建 registry)。
#
# RAW_ALIASES: 聚合平台 raw 价格行名 -> 规范 registry 名
# (price_snapshots.model 存 raw 名, registry/model_groups 用规范名,
#  与生产 modelscope 双行并存形态一致)
# ============================================================

MODEL_REGISTRY = {
    # --- deepseek 系 ---
    "deepseek-v4-pro": {
        "tier": "T0", "family": "deepseek", "modality": "multimodal",
        "capabilities": ["reasoning", "code", "math", "long_context"],
        "context_length": 1048576,
    },
    "deepseek-flash": {
        "tier": "T0", "family": "deepseek", "modality": "multimodal",
        "capabilities": ["reasoning", "code", "math", "long_context"],
        "context_length": 1048576,
    },
    "deepseek-v4-flash": {
        "tier": "T1", "family": "deepseek", "modality": "multimodal",
        "capabilities": ["reasoning", "code", "long_context"],
        "context_length": 1048576,
    },
    "deepseek-v3": {
        "tier": "T1", "family": "deepseek", "modality": "text",
        "capabilities": ["reasoning", "code", "math"],
        "context_length": 131072,
    },
    # --- glm 系 ---
    "glm-5.2": {
        "tier": "T1", "family": "glm", "modality": "text",
        "capabilities": ["reasoning", "code", "long_context"],
        "context_length": 131072,
    },
    "glm-5.3": {
        "tier": "T1", "family": "glm", "modality": "text",
        "capabilities": ["reasoning", "code", "long_context"],
        "context_length": 131072,
    },
    "glm-5.3-flash": {
        "tier": "T2", "family": "glm", "modality": "multimodal",
        "capabilities": ["general", "code"],
        "context_length": 131072,
    },
    # --- kimi 系 ---
    "kimi-k3": {
        "tier": "T0", "family": "kimi", "modality": "multimodal",
        "capabilities": ["reasoning", "code", "long_context"],
        "context_length": 1048576,
    },
    "moonshot-v1-128k": {
        "tier": "T1", "family": "kimi", "modality": "text",
        "capabilities": ["general", "long_context"],
        "context_length": 131072,
    },
    "moonshot-v1-32k": {
        "tier": "T2", "family": "kimi", "modality": "text",
        "capabilities": ["general"],
        "context_length": 32768,
    },
    # --- minimax 系 ---
    "minimax-m3": {
        "tier": "T1", "family": "minimax", "modality": "text",
        "capabilities": ["reasoning", "code", "long_context"],
        "context_length": 1048576,
    },
    "minimax-m3.1-flash-preview": {
        "tier": "T2", "family": "minimax", "modality": "multimodal",
        "capabilities": ["general", "code", "long_context"],
        "context_length": 1048576,
    },
    # --- stepfun / qwen / sensenova ---
    "step-5-preview": {
        "tier": "T0", "family": "stepfun", "modality": "multimodal",
        "capabilities": ["reasoning", "code", "long_context"],
        "context_length": 1048576,
    },
    "step-3.5-flash": {
        "tier": "T3", "family": "stepfun", "modality": "text",
        "capabilities": ["general"],
        "context_length": 131072,
    },
    "qwen2.5-72b": {
        "tier": "T2", "family": "qwen", "modality": "text",
        "capabilities": ["reasoning", "code", "math"],
        "context_length": 131072,
    },
    "sensenova-6.8-flash-lite": {
        "tier": "T3", "family": "sensenova", "modality": "text",
        "capabilities": ["general"],
        "context_length": 131072,
    },
}

RAW_ALIASES = {
    # modelscope (raw 价格行名 -> 规范名)
    "deepseek-ai/DeepSeek-V4.1-Flash": "deepseek-flash",
    "deepseek-ai/DeepSeek-V4-Pro": "deepseek-v4-pro",
    "ZhipuAI/GLM-5.2": "glm-5.2",
    # siliconflow
    "deepseek-ai/DeepSeek-V3": "deepseek-v3",
    "Qwen/Qwen2.5-72B-Instruct": "qwen2.5-72b",
    "zai-org/GLM-5.3": "glm-5.3",
    "zai-org/GLM-5.3-Flash": "glm-5.3-flash",
    "zai-org/GLM-5.2": "glm-5.2",
}

# ============================================================
# Onboarding 元数据约定 (dashboard/onboarding.py 消费):
#   category: free | paid | subscription  — 页面分区
#   display / tagline / description          — 卡片文案
#   register_url                             — 注册入口 (个人邀请链接须注明)
#   key_console_url                          — API key 控制台
#   setup_note: 可选, 配置额外要求 (如 minimax 必须填 base_url)
#   invite: 注册链接是维护者邀请链接时写简短说明
# ============================================================

CHANNELS = [
    # ========== 免费渠道 (推荐先配) ==========
    {
        "key": "sensenova",
        "name": "sensenova_official",
        "provider": "sensenova",
        "base_url": os.getenv("SENSENOVA_BASE_URL", "https://token.sensenova.cn/v1"),
        "priority": 5,
        "env_var": "SENSENOVA_API_KEY",
        "qpm": 30,
        "onboarding": {
            "category": "free",
            "display": "商汤日日新 SenseNova",
            "tagline": "官方公测期免费调用",
            "description": (
                "TokenPlan 官方页面明确「公测期完全免费开放」（60,000 积分/5 小时，"
                "特殊模型除外）：公测期免费调用主力模型。共享吞吐限流，高峰期可能 429"
                " — AIMC 会自动冷却重试，无需人工处理。"
                "可用模型: sensenova-6.8-flash-lite / deepseek-flash / glm-5.2 / kimi-k3。"
            ),
            "register_url": "https://platform.sensenova.cn/console/keys",
            "key_console_url": "https://platform.sensenova.cn/console/keys",
            "invite": None,
        },
        "prices": [
            ("sensenova-6.8-flash-lite", "normal", "全天", 0.0, 0.0),
            ("deepseek-flash", "normal", "全天", 0.0, 0.0),
            ("deepseek-v4-flash", "normal", "全天", 0.0, 0.0),
            ("glm-5.2", "normal", "全天", 0.0, 0.0),
            ("kimi-k3", "normal", "全天", 0.0, 0.0),
            ("sensenova-u1.5-fast", "normal", "全天", 0.0, 0.0),
            ("sensenova-u1.5-lite", "normal", "全天", 0.0, 0.0),
        ],
    },
    {
        "key": "modelscope",
        "name": "modelscope_inference",
        "provider": "modelscope",
        "base_url": os.getenv("MODELSCOPE_BASE_URL", "https://api-inference.modelscope.cn/v1"),
        "priority": 10,
        "env_var": "MODELSCOPE_API_KEY",
        "qpm": 30,
        "onboarding": {
            "category": "free",
            "display": "魔搭 ModelScope API-Inference",
            "tagline": "注册领魔粒, 免费调大参数模型",
            "description": (
                "魔搭社区的免费推理服务：注册并绑定阿里云账号后，用全站激励积分"
                "「魔粒」免费调用 DeepSeek-V4.1-Flash / GLM-5.2 / Qwen3-235B 等"
                "大参数模型；登录、做社区任务可继续获得魔粒。免费额度动态调整、"
                "超额自动限流（AIMC 冷却自愈），可用模型以官网当日列表为准。"
            ),
            "register_url": "https://www.modelscope.cn/register?inviteCode=守一&invitorName=shouyi01",
            "key_console_url": "https://modelscope.cn/my/myaccesstoken",
            "invite": "此为维护者邀请链接, 注册成功会给维护者少量魔粒奖励, 不影响你的额度",
            "setup_note": "需要绑定阿里云账号（免费推理由阿里云提供算力）",
        },
        "prices": [
            ("deepseek-ai/DeepSeek-V4.1-Flash", "normal", "全天", 0.0, 0.0),
            ("deepseek-ai/DeepSeek-V4-Pro", "normal", "全天", 0.0, 0.0),
            ("ZhipuAI/GLM-5.2", "normal", "全天", 0.0, 0.0),
            ("Qwen/Qwen3-Next-80B-A3B-Instruct", "normal", "全天", 0.0, 0.0),
            ("Qwen/Qwen3-235B-A22B", "normal", "全天", 0.0, 0.0),
        ],
    },
    {
        "key": "siliconflow",
        "name": "siliconflow_aggregator",
        "provider": "siliconflow",
        "base_url": "https://api.siliconflow.cn/v1",
        "priority": 1,
        "env_var": "SILICONFLOW_API_KEY",
        "qpm": 100,
        "onboarding": {
            "category": "free",
            "display": "硅基流动 SiliconFlow",
            "tagline": "部分模型免费, 1 key 通吃多家",
            "description": (
                "国内主流第三方 API 聚合平台：部分小参数开源模型免费（官方文档："
                "完成实名认证后可使用全部免费模型，免费模型账单费用为 0）；"
                "新用户注册赠送代金券（金额以官网当前活动为准）。"
                "1 个 key 可调 DeepSeek / GLM / Qwen / Kimi 等国内主流模型"
                "（含 DeepSeek-V4-Flash、GLM-5.3 系列等最新模型），"
                "最适合作为入门第一站。"
            ),
            "register_url": "https://cloud.siliconflow.cn/i/wnlNQHpA",
            "key_console_url": "https://cloud.siliconflow.cn/account/ak",
            "invite": "此为维护者邀请链接, 你注册后维护者获得少量余额返利, 不影响你的权益",
            "models_display": [
                "deepseek-ai/DeepSeek-V4-Flash",
                "zai-org/GLM-5.3-Flash",
                "zai-org/GLM-5.3",
                "deepseek-ai/DeepSeek-V3",
                "Qwen/Qwen2.5-72B-Instruct",
            ],
        },
        "prices": [
            # 对齐硅基流动官方价格页 (2026-10-14 核对 www.siliconflow.cn/pricing)
            # DeepSeek-V3 旧目录 2.0/2.0 为错价, 修正为 2.0/8.0 (与生产一致)
            ("deepseek-ai/DeepSeek-V3", "normal", "全天", 2.0, 8.0),
            ("Qwen/Qwen2.5-72B-Instruct", "normal", "全天", 4.13, 4.13),
            # 2026-10-14 新增: GLM-5.3 系列 + Hy4 preview (官方页实价, 含缓存价)
            ("zai-org/GLM-5.3", "normal", "全天", 8.0, 28.0, 2.0),
            ("zai-org/GLM-5.3-Flash", "normal", "全天", 0.8, 2.8, 0.23),
            ("zai-org/GLM-5.2", "normal", "全天", 8.0, 28.0),
            ("tencent/Hy4-preview", "normal", "全天", 6.0, 18.0, 0.30),
        ],
    },

    # ========== 官方付费渠道 ==========
    {
        "key": "deepseek",
        "name": "deepseek_official",
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com/v1",
        "priority": 10,
        "env_var": "DEEPSEEK_API_KEY",
        "qpm": 60,
        "onboarding": {
            "category": "paid",
            "display": "DeepSeek 官方",
            "tagline": "高峰/闲时分段计价, 闲时半价",
            "description": (
                "DeepSeek 官方 API：V4-Pro / V4-Flash 按时段计价（工作日高峰与"
                "非高峰差一倍，周末全天闲时价），缓存命中输入再打折。充值后即用。"
            ),
            "register_url": "https://platform.deepseek.com/",
            "key_console_url": "https://platform.deepseek.com/api_keys",
            "invite": None,
        },
        "prices": [
            # 生产权威价 (2026-10-14 核对 aliy-hy price_snapshots; 官方定价页:
            # api-docs.deepseek.com 高峰=工作日09:00-12:00/14:00-18:00, 空闲=其余+周末)
            # v4-flash/vision-exp 行已移除 — 官方渠道现仅承接 v4-pro 与 deepseek-flash
            ("deepseek-v4-pro", "peak", "工作日09:00-12:00", 9.0, 27.0, 0.30),
            ("deepseek-v4-pro", "peak", "工作日14:00-18:00", 9.0, 27.0, 0.30),
            ("deepseek-v4-pro", "offpeak", "非高峰全天", 4.5, 13.5, 0.15),
            ("deepseek-flash", "peak", "工作日09:00-12:00", 2.0, 8.0, 0.04),
            ("deepseek-flash", "peak", "工作日14:00-18:00", 2.0, 8.0, 0.04),
            ("deepseek-flash", "offpeak", "非高峰全天", 1.0, 4.0, 0.02),
        ],
    },
    {
        "key": "glm",
        "name": "glm_bigmodel",
        "provider": "glm",
        "base_url": "https://open.bigmodel.cn/api/coding/paas/v4",
        "priority": 5,
        "env_var": "GLM_API_KEY",
        "qpm": 30,
        "onboarding": {
            "category": "paid",
            "display": "智谱 GLM BigModel",
            "tagline": "GLM-5.3 系列, coding plan 积分制",
            "description": (
                "智谱开放平台：GLM-5.3 / GLM-5.3-Flash 为当前主力模型。"
                "AIMC 统一使用 coding plan 端点（/api/coding/paas/v4）——注册后在"
                "控制台开通 coding plan（Lite 档含免费积分：2000 积分/5h + 10000 积分/周，"
                "仅限 GLM-5.3 系列）即可按订阅额度调用；目录中的 0 元价 = "
                "订阅额度内的边际成本口径，超出额度或未订阅部分按官方按量价计费。"
            ),
            "register_url": "https://open.bigmodel.cn/",
            "key_console_url": "https://bigmodel.cn/usercenter/profit-metric",
            "invite": None,
            "models_display": ["glm-5.3", "glm-5.3-flash"],
        },
        "prices": [
            # 0 元 = coding plan 订阅边际成本口径 (与生产 glm_bigmodel 形态一致;
            # 未订阅/超额部分按官方按量价计费, 成本页以官方标价双轨估算)
            ("glm-5.3", "normal", "全天", 0.0, 0.0),
            ("glm-5.3-flash", "normal", "全天", 0.0, 0.0),
        ],
    },
    {
        "key": "kimi",
        "name": "kimi_official",
        "provider": "kimi",
        "base_url": "https://api.moonshot.cn/v1",
        "priority": 5,
        "env_var": "KIMI_API_KEY",
        "qpm": 30,
        "onboarding": {
            "category": "paid",
            "display": "Kimi (Moonshot 开放平台)",
            "tagline": "kimi-k3 系列 + 长上下文",
            "description": (
                "Moonshot 开放平台：当前主力为 kimi-k3 系列（原生多模态、1M 上下文），"
                "另有 moonshot-v1-32k/128k 长上下文模型仍在售（按上下文档位计价）。"
                "模型与价格以官方定价页为准。"
            ),
            "register_url": "https://platform.moonshot.cn/",
            "key_console_url": "https://platform.moonshot.cn/console/api-keys",
            "invite": None,
            "models_display": ["kimi-k3", "moonshot-v1-128k", "moonshot-v1-32k"],
        },
        "prices": [
            ("moonshot-v1-128k", "normal", "全天", 6.0, 6.0),
            ("moonshot-v1-32k", "normal", "全天", 3.0, 3.0),
        ],
    },
    {
        "key": "minimax",
        "name": "minimax_compat",
        "provider": "minimax",
        "base_url": os.getenv("MINIMAX_BASE_URL", "https://api.minimaxi.com/v1"),
        "priority": 5,
        "env_var": "MINIMAX_API_KEY",
        "qpm": 60,
        "onboarding": {
            "category": "paid",
            "display": "MiniMax",
            "tagline": "M3 系列, M Plan 订阅, 原生多模态",
            "description": (
                "MiniMax 开放平台（OpenAI 兼容）：当前主力 minimax-m3 / "
                "m3.1-flash-preview，全系原生多模态。订阅制为 M Plan（token 包），"
                "AIMC 以订阅边际成本 0 元参与路由；价格以官方为准。"
            ),
            "register_url": "https://platform.minimaxi.com/",
            "key_console_url": "https://platform.minimaxi.com/user-center/basic-information/interface-key",
            "invite": None,
            "models_display": ["minimax-m3", "minimax-m3.1-flash-preview"],
        },
        "prices": [
            # m2.7 / m2.7-highspeed 已移除 — 官方公布近期下架 (2026-10 用户确认)
            ("minimax-m3", "normal", "全天", 0.0, 0.0),
            ("minimax-m3.1-flash-preview", "normal", "全天", 0.0, 0.0),
        ],
    },
    {
        "key": "stepfun",
        "name": "stepfun_official",
        "provider": "stepfun",
        "base_url": "https://api.stepfun.com/v1",
        "priority": 5,
        "env_var": "STEPFUN_API_KEY",
        "qpm": 30,
        "onboarding": {
            "category": "paid",
            "display": "阶跃星辰 StepFun",
            "tagline": "step-5-preview 1M 上下文",
            "description": (
                "阶跃开放平台：step-5-preview（1M 上下文 / 多模态输入），"
                "缓存命中输入 0.35 元/百万 tokens。"
            ),
            "register_url": "https://platform.stepfun.com/",
            "key_console_url": "https://platform.stepfun.com/interface-key",
            "invite": None,
        },
        "prices": [
            ("step-3.5-flash", "normal", "全天", 0.0, 0.0),
            ("step-5-preview", "normal", "全天", 7.0, 20.0, 0.35),
        ],
    },

    {
        # 不进初始化配置页 (用户未列; 保留 manual_import .env 引导兼容)
        "key": "volcengine",
        "name": "volcengine_ark",
        "provider": "volcengine",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "priority": 5,
        "env_var": "VOLCENGINE_API_KEY",
        "qpm": 60,
        "onboarding": {"hidden": True},
        "prices": [
            ("doubao-seed-2-0-pro-260215", "normal", "全天", 0.8, 2.0),
            ("doubao-seed-1-6-thinking-250615", "normal", "全天", 4.0, 16.0),
        ],
    },
    {
        # 不进初始化配置页 (用户未列; 保留 manual_import .env 引导兼容)
        "key": "hunyuan",
        "name": "hunyuan_official",
        "provider": "hunyuan",
        "base_url": "https://hunyuan.tencent.com/v1",
        "priority": 5,
        "env_var": "HUNYUAN_API_KEY",
        "qpm": 30,
        "onboarding": {"hidden": True},
        "prices": [
            ("hunyuan-pro", "normal", "全天", 0.0, 0.0),
        ],
    },

    # ========== 订阅制聚合 ==========
    {
        "key": "taotoken",
        "name": "taotoken_coding",
        "provider": "taotoken",
        "base_url": os.getenv("TAOTOKEN_BASE_URL", "https://api.taotoken.net/coding/v1"),
        "priority": 9,
        "env_var": "TAOTOKEN_API_KEY",
        "qpm": 30,
        "onboarding": {
            "category": "subscription",
            "display": "TaoToken",
            "tagline": "订阅制聚合, Coding Plan Pro 量大管饱",
            "description": (
                "第三方 coding plan 会员聚合，分 Token Plan 与 Coding Plan 两类订阅。"
                "综合推荐 Coding Plan Pro 档（Lite 档已售罄）：约 2000 次/5h + "
                "15000 次/周，按调用次数而非 token 计——大请求量场景比较划算、"
                "量大管饱。订阅后 glm-5.3-flash / deepseek-v4-flash 以 0 元边际成本"
                "参与路由。适合大请求量用户。"
            ),
            "register_url": "https://taotoken.net/coding-plan?u=inv_ua31j1nkx1i7ch5w&utm_source=tt_invite",
            "key_console_url": "https://taotoken.net/",
            "invite": "此为维护者邀请链接",
        },
        "prices": [
            ("glm-5.3-flash", "normal", "全天", 0.0, 0.0),
            ("deepseek-v4-flash", "normal", "全天", 0.0, 0.0),
        ],
    },
]

# 初始化配置页分区标题 (category -> 标题+说明)
CATEGORY_META = {
    "free": {
        "title": "免费渠道 (推荐先配)",
        "hint": "先配一个免费渠道即可跑通全链路; 之后随时可在「运维 → 账号管理」追加",
    },
    "paid": {
        "title": "官方付费渠道",
        "hint": "按量计费, 价格已按官方定价页录入",
    },
    "subscription": {
        "title": "订阅制聚合",
        "hint": "包月/包次订阅, 订阅后按边际成本参与路由",
    },
}

# PR 引导文案 (支持更多服务商) — 页面只放 GitHub 链接 + 文件指引,
# 条目格式以仓库内 channels_catalog.py 现有条目为准 (避免页内大段代码块)
PR_GUIDE = {
    "repo_url": "https://github.com/xyshanren/aimc",
    "file": "channels_catalog.py",
}


def get_by_key(key: str):
    for cfg in CHANNELS:
        if cfg["key"] == key:
            return cfg
    return None
