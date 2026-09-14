"""手动录入渠道/账号/价格脚本

Phase 1 使用：在爬虫未就绪前，手动录入渠道信息。

设计原则：
- **不在代码里写任何 api_key**（避免 commit 泄露）
- 从环境变量读：DEEPSEEK_API_KEY / QWEN_API_KEY / GLM_API_KEY / KIMI_API_KEY / ...
- 没填对应 env → 跳过该渠道（不创建占位 account）

支持的渠道（9 个）:
- 5 家直连: deepseek / qwen / glm / kimi / minimax
- 1 个第三方聚合: siliconflow (推荐 1 个 key 通吃 5 家 + 多家国内 model)
- 3 个国内直连官方平台: volcengine (字节方舟) / hunyuan (腾讯混元) / stepfun (阶跃)

用法：
    1. 编辑 .env，至少填一个厂商的 key（推荐 SILICONFLOW_API_KEY 一码通）
    2. python scripts/manual_import.py

借鉴 hermes-tray：env-first，避免在 .py 里出现任何 key 字面量。

V4.6.4 重要区别（2026-08-13）：
- 本脚本的 *_API_KEY 环境变量跟 .env 里 OPENAI_API_KEY / OPENAI_BASE_URL /
  PARSE_MODEL 是两套独立配置，互不重叠：
  - 本脚本：创建 mini渠道 + 写入 db.accounts.ciphertext（面向业务网关）
  - 上面 OPENAI_*：intelligence/parser.py 爬取页面后的 AI 结构化提取使用
- 例如：.env 里可以同时配 MINIMAX_API_KEY（供本脚本创建 minimax_compat 渠道）
  和 OPENAI_API_KEY=<同一个 MiniMax token> + OPENAI_BASE_URL=https://api.minimaxi.com/v1
  （供 parser.py 用 MiniMax-M2.7 提取页面内容）。同一个 key 可能要配两份。

V4.4 引导：多账号场景下，推荐使用 scripts/add_account.py CLI：
    python scripts/add_account.py \\
        --channel deepseek \\
        --name deepseek_02 \\
        --api-key sk-new-key-xxx \\
        --quota-total 100000 \\
        --expires-at 2026-11-08 \\
        --notes '新账号'
本脚本仅供首次 init（备份兼容路径）；运行时增量账号统一走 CLI。
"""

import asyncio
import os
import sys
from pathlib import Path

# 将项目根目录加入 sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

# 加载 .env（让 os.environ 有值）
load_dotenv()

from db.engine import async_session_factory
from db.init_db import init_db
from db import crud
from db.normalizers import normalize_model_id, normalize_family  # Phase 4 v4.3.1 (2026-08-13): 防御性归一化
from config import settings  # API-Sec-1: 读 AIMC_MASTER_KEY


# 渠道元数据（不含 api_key）— 厂商 URL 公开信息
# Phase 2E: 9 个渠道 (5 家直连 + 1 聚合 + 3 国内直连官方平台)
#   2026-07-25 加: kimi, siliconflow, volcengine, hunyuan, stepfun
CHANNELS = [
    # ========== 5 家直连 (国内模型厂商官方) ==========
    {
        "key": "deepseek",
        "name": "deepseek_official",
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com/v1",
        "priority": 10,
        "env_var": "DEEPSEEK_API_KEY",
        "qpm": 60,
        # Phase 4 v4.3.1 (2026-08-13): 改用 V4 系列 model (跟 1005111 commit 对齐)
        # 历史 deepseek-chat / deepseek-reasoner 是 V4.4 时代老 model,
        # 官方 API 当前 (2026-08) 只支持 V4 系列.
        "prices": [
            # V4.9.5: 对齐 DeepSeek 官方价格 (https://api-docs.deepseek.com/zh-cn/quick_start/pricing)
            # 高峰: 工作日 09:00-12:00, 14:00-18:00 (北京时间)
            # 空闲: 其余时段 + 周末全天 (价格 = 高峰 × 0.5)
            # 价格单位: 元/百万 tokens; 第 6 个元素 = cached_input_price (缓存命中输入价)
            # deepseek-v4-pro: 高峰 input=9.0/cache=0.30, output=27.0
            ("deepseek-v4-pro", "peak", "工作日09:00-12:00", 9.0, 27.0, 0.30),
            ("deepseek-v4-pro", "peak", "工作日14:00-18:00", 9.0, 27.0, 0.30),
            ("deepseek-v4-pro", "offpeak", "非高峰全天", 4.5, 13.5, 0.15),
            # deepseek-v4-flash: 高峰 input=3.0/cache=0.10, output=9.0
            ("deepseek-v4-flash", "peak", "工作日09:00-12:00", 3.0, 9.0, 0.10),
            ("deepseek-v4-flash", "peak", "工作日14:00-18:00", 3.0, 9.0, 0.10),
            ("deepseek-v4-flash", "offpeak", "非高峰全天", 1.5, 4.5, 0.05),
            # deepseek-v4-flash-vision-exp: 视觉实验版, 价格同 v4-flash
            # 图片转 token 后与文本合并计费
            ("deepseek-v4-flash-vision-exp", "peak", "工作日09:00-12:00", 3.0, 9.0, 0.10),
            ("deepseek-v4-flash-vision-exp", "peak", "工作日14:00-18:00", 3.0, 9.0, 0.10),
            ("deepseek-v4-flash-vision-exp", "offpeak", "非高峰全天", 1.5, 4.5, 0.05),
        ],
    },
    {
        "key": "qwen",
        "name": "qwen_bailian",
        "provider": "qwen",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "priority": 5,
        "env_var": "QWEN_API_KEY",
        "qpm": 30,
        "prices": [
            ("qwen-max", "normal", "全天", 2.0, 6.0),
            ("qwen-plus", "normal", "全天", 0.8, 2.0),
        ],
    },
    {
        "key": "glm",
        "name": "glm_bigmodel",
        "provider": "glm",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "priority": 5,
        "env_var": "GLM_API_KEY",
        "qpm": 30,
        "prices": [
            ("glm-4-plus", "normal", "全天", 7.0, 7.0),
            ("glm-4-flash", "normal", "全天", 0.1, 0.1),
        ],
    },
    {
        # Phase 2E: Kimi (Moonshot 官方) — 2026-07-25 补
        "key": "kimi",
        "name": "kimi_official",
        "provider": "kimi",
        "base_url": "https://api.moonshot.cn/v1",
        "priority": 5,
        "env_var": "KIMI_API_KEY",
        "qpm": 30,
        # Phase 4 v4.3.1 (2026-08-13): 保持 cfg model 跟实际 Moonshot API 一致.
        # model_id 命名以 "moonshot-" 开头 (family=moonshot, 跟 registry 对齐).
        # 注意: 这俩 model 不在 model_registry (registry 只 seed 了 moonshot-v1-8k),
        # 跑 check_group_members_consistency 时会报 "model 不在 registry",
        # 如需加可手动 seed 进去.
        "prices": [
            ("moonshot-v1-128k", "normal", "全天", 6.0, 6.0),
            ("moonshot-v1-32k", "normal", "全天", 3.0, 3.0),
        ],
    },
    {
        # Phase 2D-3: MiniMax 渠道 (2026-07-24, V4.6.4 修正: 用 MiniMax 官方 model 名)
        # MiniMax OpenAI-compatible 代理 — base_url 需用户填 (留 placeholder)
        # 用 MINIMAX_BASE_URL 单独配 endpoint（其他渠道用硬编码 base_url）
        # V4.6.4 fix: prices 用 MiniMax 官方当前 8 个 model (取代错误的 gpt-4o-mini placeholder)
        #   - MiniMax-M3 / MiniMax-M2.7 / MiniMax-M2.7-highspeed / MiniMax-M2.5
        #   - MiniMax-M2.5-highspeed / MiniMax-M2.1 / MiniMax-M2.1-highspeed / MiniMax-M2
        # 价格默认 0.0 (placeholder) - 等真实计费后改
        "key": "minimax",
        "name": "minimax_compat",
        "provider": "minimax",
        "base_url": os.getenv("MINIMAX_BASE_URL", "https://PLACEHOLDER.minimax/v1"),
        "priority": 5,
        "env_var": "MINIMAX_API_KEY",
        "qpm": 60,
        "prices": [
            # V4.6.4: MiniMax 官方当前 8 个 model (从 platform.minimaxi.com/docs 查, 2026-08-08)
            # Phase 4 v4.3.1 (2026-08-13): model_id 全小写 namespace (跟 450499c 对齐).
            # 跟 model_registry / seed_model_groups / seed_model_cards 一致.
            ("minimax-m3",              "normal", "全天", 0.0, 0.0),
            ("minimax-m2.7",            "normal", "全天", 0.0, 0.0),
            ("minimax-m2.7-highspeed",  "normal", "全天", 0.0, 0.0),
            ("minimax-m2.5",            "normal", "全天", 0.0, 0.0),
            ("minimax-m2.5-highspeed",  "normal", "全天", 0.0, 0.0),
            ("minimax-m2.1",            "normal", "全天", 0.0, 0.0),
            ("minimax-m2.1-highspeed",  "normal", "全天", 0.0, 0.0),
            ("minimax-m2",              "normal", "全天", 0.0, 0.0),
        ],
    },

    # ========== 1 个第三方聚合 (推荐 1 个 key 通吃) ==========
    {
        # Phase 2E: 硅基流动 SiliconFlow (2026-07-25)
        # 国内最大的第三方 API 聚合平台, OpenAI-compatible
        # 1 个 key 可以调: DeepSeek/Qwen/GLM/Kimi/MiniMax/Doubao/Hunyuan/StepFun/InternVL/MiniCPM/...
        # 推荐: 个人国内使用首选, 一个 key 试通多家
        "key": "siliconflow",
        "name": "siliconflow_aggregator",
        "provider": "siliconflow",
        "base_url": "https://api.siliconflow.cn/v1",
        "priority": 1,  # 优先级最低 — 但有 fallback 价值
        "env_var": "SILICONFLOW_API_KEY",
        "qpm": 100,  # SiliconFlow 免费档 QPM 高
        "prices": [
            # 价格 placeholder — 聚合平台价格不固定, 实际从 SiliconFlow 定价页查
            # 常用 model 已在 OpenCompass 4 榜上榜, 这里只列 1-2 个代表
            ("deepseek-ai/DeepSeek-V3", "normal", "全天", 2.0, 2.0),
            ("Qwen/Qwen2.5-72B-Instruct", "normal", "全天", 4.13, 4.13),
        ],
    },

    # ========== 3 个国内直连官方平台 ==========
    {
        # Phase 2E: 火山方舟 VolcEngine Ark (字节官方) (2026-07-25)
        # 字节 Doubao 官方 endpoint
        "key": "volcengine",
        "name": "volcengine_ark",
        "provider": "volcengine",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "priority": 5,
        "env_var": "VOLCENGINE_API_KEY",
        "qpm": 60,
        "prices": [
            ("doubao-seed-2-0-pro-260215", "normal", "全天", 0.8, 2.0),
            ("doubao-seed-1-6-thinking-250615", "normal", "全天", 4.0, 16.0),
        ],
    },
    {
        # Phase 2E: 腾讯混元 Hunyuan (2026-07-25)
        # 腾讯官方 endpoint
        "key": "hunyuan",
        "name": "hunyuan_official",
        "provider": "hunyuan",
        "base_url": "https://hunyuan.tencent.com/v1",  # 用户用实际 endpoint
        "priority": 5,
        "env_var": "HUNYUAN_API_KEY",
        "qpm": 30,
        "prices": [
            ("hunyuan-pro", "normal", "全天", 0.0, 0.0),  # placeholder
        ],
    },
    {
        # Phase 2E: 阶跃 StepFun (2026-07-25)
        # 阶跃官方 endpoint
        "key": "stepfun",
        "name": "stepfun_official",
        "provider": "stepfun",
        "base_url": "https://api.stepfun.com/v1",
        "priority": 5,
        "env_var": "STEPFUN_API_KEY",
        "qpm": 30,
        "prices": [
            ("step-3.5-flash", "normal", "全天", 0.0, 0.0),  # placeholder
            ("step-1o", "normal", "全天", 0.0, 0.0),  # placeholder
        ],
    },
    {
        # V4.6.4: 商汤日日新 SenseNova (2026-08-09)
        # 官方推荐 base_url: https://token.sensenova.cn/v1 (公测 token plan 入口)
        # 历史默认 api.sensenova.cn 错误 (少数 model 在该域名 1214 modelCode不存在)
        # env SENSENOVA_BASE_URL 可运行时覆盖 db (gateway/router.py _resolve_effective_base_url)
        # 鉴权: 标准 Bearer token (sk- 开头), 跟其他渠道一样
        # model (V4.6.4 update):
        #   - SenseChat: 移除 (上游已失效, code 5 model not found)
        #   - sensenova-6.8-flash-lite: 公测 token plan 主力 (8/8 取代 6.7)
        #   - sensenova-u1-fast: U1 加速版
        #   - deepseek-v4-flash: 公测免费 (跨渠道 model_id)
        #   - sensenova-6.7-flash-lite: 保留到 8/31, deprecated (官方重定向将失效)
        #   - glm-5.2: 公测免费 (跨渠道, 免费第二渠道 — cheapest 策略下 0.0 胜过 siliconflow 8.0)
        "key": "sensenova",
        "name": "sensenova_official",
        "provider": "sensenova",
        "base_url": os.getenv("SENSENOVA_BASE_URL", "https://token.sensenova.cn/v1"),
        "priority": 5,
        "env_var": "SENSENOVA_API_KEY",
        "qpm": 30,
        "prices": [
            ("sensenova-6.8-flash-lite", "normal", "全天", 0.0, 0.0),  # 主力
            ("sensenova-u1-fast", "normal", "全天", 0.0, 0.0),  # 加速版
            ("deepseek-v4-flash", "normal", "全天", 0.0, 0.0),  # 公测免费 (跨渠道)
            ("sensenova-6.7-flash-lite", "normal", "全天", 0.0, 0.0),  # 8/31 后失效, 保留兼容
            ("glm-5.2", "normal", "全天", 0.0, 0.0),  # 公测免费 (跨渠道, 免费第二渠道)
        ],
    },
    {
        # V6.5+ (2026-09-14): ModelScope 魔搭 API-Inference (免费渠道扩容, 调研 §2.6.1)
        # OpenAI 兼容: https://api-inference.modelscope.cn/v1, 鉴权 Bearer ms- token
        # 免费额度动态调整 (官方无固定 RPD) — 超额 429/空壳 200 → 冷却自愈 (同 sensenova 模式)
        # 价格行 = raw 名直录 (同 siliconflow 聚合平台模式, price_snapshots 行
        # 动态决定渠道候选); 全 0 元 (免费额度内)
        # ⚠️ 模型行来自 2026-09-14 shouyi01 账号全量实测 (API-Inference可用模型实测
        # _2026-09-14.md): 只录大参数可用行, 小模型/需流式/不可用行不录。
        #   - DeepSeek-V4.1-Flash: 实测直接可用 (注意! ModelScope 的 V4.1-Flash ≈
        #     官方 deepseek-flash, 与 sensenova 行 deepseek-v4-flash 是不同代 —
        #     各自独立行, 不互相覆盖)
        #   - DeepSeek-V4-Pro / -0813: 实测直接可用 (v4-pro 第二免费供给)
        #   - ZhipuAI/GLM-5.2: 实测直接可用 (glm-5.2 跨渠道第三供给)
        #   - Qwen3-Next-80B-A3B-Instruct / Qwen3-235B-A22B: 大参数新面孔,
        #     暂不入 tier 组, 观察后定
        #   MiniMax-M3 / GLM-4.7-Flash / Hunyuan-Hy3 等实测不可用或 1305 过载, 不录
        "key": "modelscope",
        "name": "modelscope_inference",
        "provider": "modelscope",
        "base_url": os.getenv("MODELSCOPE_BASE_URL", "https://api-inference.modelscope.cn/v1"),
        "priority": 10,  # 最低档兜底 — 已有 0 元渠道 (sensenova) 先走
        "env_var": "MODELSCOPE_API_KEY",
        "qpm": 30,
        "prices": [
            ("deepseek-ai/DeepSeek-V4.1-Flash", "normal", "全天", 0.0, 0.0),
            ("deepseek-ai/DeepSeek-V4-Pro", "normal", "全天", 0.0, 0.0),
            ("ZhipuAI/GLM-5.2", "normal", "全天", 0.0, 0.0),
            ("Qwen/Qwen3-Next-80B-A3B-Instruct", "normal", "全天", 0.0, 0.0),
            ("Qwen/Qwen3-235B-A22B", "normal", "全天", 0.0, 0.0),
        ],
    },
]


async def _price_row_exists(session, channel_id: int, model: str, price_type: str, time_range: str) -> bool:
    """检查 (channel, model, price_type, time_range) 价格行是否已存在"""
    from sqlalchemy import select
    from db.models import PriceSnapshot

    stmt = select(PriceSnapshot.id).where(
        PriceSnapshot.channel_id == channel_id,
        PriceSnapshot.model == model,
        PriceSnapshot.price_type == price_type,
        PriceSnapshot.time_range == time_range,
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def _upsert_price_row(session, channel_id: int, price_row: tuple) -> None:
    """单条价格行 upsert (幂等)。

    V4.9.5: 支持可选 cached_input_price (第 6 个元素)
    旧格式: (model, price_type, time_range, in_p, out_p)
    新格式: (model, price_type, time_range, in_p, out_p, cached_input_price)

    V6.5+ (2026-09-14): create → upsert — 幂等修复。旧 create 在重跑时对
    已存在行抛 UNIQUE 约束 (生产实证: 全量重跑在 stepfun 行中断, 后续渠道
    全部漏建); upsert 语义与 crud.upsert_price_snapshot 一致 (值以 cfg 为准)。
    """
    model, price_type, time_range, in_p, out_p, *rest = price_row
    cached_price = rest[0] if rest else None

    # V6.5+ (2026-09-14): 聚合平台 raw 名 (含大写/斜杠命名空间, e.g.
    # "deepseek-ai/DeepSeek-V4.1-Flash") 不过 normalize_model_id — 归一化会把
    # 上游 raw 名小写化, 请求时上游 404。与 siliconflow 的 api sync (raw 名直录,
    # _siliconflow_sync.py) 同口径: price_snapshots.model 存上游原始 id,
    # usage_logs/路由透传同值。仅对无命名空间分隔符的短名保留归一化防御。
    if "/" not in model:
        model_norm = normalize_model_id(model)
        assert model_norm == model, (
            f"manual_import.py cfg 里 model='{model}' 应已归一化为 '{model_norm}'"
        )
    else:
        model_norm = model
    await crud.upsert_price_snapshot(
        session,
        channel_id=channel_id,
        model=model_norm,
        input_price=in_p,
        output_price=out_p,
        source="manual",
        price_type=price_type,
        time_range=time_range,
    )
    if cached_price is not None:
        # upsert_price_snapshot 不含 cached_input_price 列 — 新行补写
        from sqlalchemy import select
        from db.models import PriceSnapshot
        row = (await session.execute(
            select(PriceSnapshot).where(
                PriceSnapshot.channel_id == channel_id,
                PriceSnapshot.model == model_norm,
                PriceSnapshot.price_type == price_type,
                PriceSnapshot.time_range == time_range,
            )
        )).scalar_one_or_none()
        if row is not None and row.cached_input_price is None:
            row.cached_input_price = cached_price


def _clean_env_value(value: str) -> str:
    """跟 scripts/check_env.py 保持一致: 清洗行内注释 ('KEY=  # 注释')"""
    v = (value or "").strip()
    if v.lstrip().startswith("#"):
        return ""
    if " #" in v:
        v = v.split(" #", 1)[0].rstrip()
    return v


async def import_one_channel(session, cfg: dict) -> bool:
    """导入单个渠道（如果 env 有 api_key）

    API-Sec-1: api_key 加密存 ciphertext, api_key 字段留空 (deprecated)

    Returns:
        True = 创建了 account，False = 跳过（env 缺 key / channel 已存在 / master key 未配）
    """
    raw = os.getenv(cfg["env_var"], "")
    api_key = _clean_env_value(raw)
    if not api_key or api_key.startswith("sk-your-"):
        print(f"[skip] {cfg['name']}: {cfg['env_var']} 未配置")
        return False

    # V4.6.4: minimax_compat 是 OpenAI 代理, endpoint 必须显式配
    # 如果 .env 没 MINIMAX_BASE_URL, base_url 落到 PLACEHOLDER 默认值
    # -> 创建出来的 channel 请求会发到 PLACEHOLDER.minimax (404)
    # 修复: 强制要求 MINIMAX_BASE_URL 配好 (避免静默错误)
    if cfg["provider"] == "minimax":
        base_url = _clean_env_value(os.getenv("MINIMAX_BASE_URL", ""))
        if not base_url or "PLACEHOLDER" in base_url:
            print(
                f"[skip] {cfg['name']}: MINIMAX_BASE_URL 未配置或为 PLACEHOLDER"
            )
            print(
                f"       修复: nano ~/.env, 加 MINIMAX_BASE_URL=https://你的代理endpoint/v1"
            )
            return False

    # API-Sec-1: master key 校验
    if not settings.aimc_master_key:
        print(f"[skip] {cfg['name']}: AIMC_MASTER_KEY 未配置, 无法加密")
        print(f"       运行: python scripts/gen_master_key.py, 把输出贴到 .env")
        return False

    # 幂等性: 同名 channel 已存在就 skip (老数据由 migrate_encrypt_keys.py 迁移)
    from sqlalchemy import select
    from db.models import Channel as ChannelModel, Account as AccountModel
    existing_ch = (await session.execute(
        select(ChannelModel).where(ChannelModel.name == cfg["name"])
    )).scalar_one_or_none()
    if existing_ch is not None:
        # V6.5.1 (2026-09-14): 幂等判断按"该渠道下是否存在任意账号"（不看 status）—
        # 生产实证: 用户在运维页禁用账号后重跑本脚本, 旧判断只认 active 找不到账号,
        # 落入 recover 分支用同一把 key 重建同名 active 账号, 绕过了禁用意图
        # (glm/stepfun 各多出一条 glm_01/stepfun_01)。disabled 账号同样算已存在。
        existing_acc = (await session.execute(
            select(AccountModel).where(
                AccountModel.channel_id == existing_ch.id,
            )
        )).scalars().first()
        if existing_acc is not None:
            # V6.5+ (2026-09-14): channel+account 已存在 ≠ 价格行已齐 —
            # 生产实证: 首次运行曾因 UNIQUE 中断留下 0 价格行渠道, 重跑被
            # 此处 skip 挡住永远补不上。补齐缺失价格行后再返回 (upsert 幂等)。
            print(f"[skip] {cfg['name']}: channel + account 已存在 (channel_id={existing_ch.id}), 如需更新 key 走 dashboard")
            missing = [
                row for row in cfg["prices"]
                if not await _price_row_exists(session, existing_ch.id, row[0], row[1], row[2])
            ]
            if missing:
                print(f"[recover] {cfg['name']}: 补齐缺失价格行 {len(missing)}/{len(cfg['prices'])}")
                for row in missing:
                    await _upsert_price_row(session, existing_ch.id, row)
                await session.commit()
            return False
        # channel 在但一条账号都没有 (真·老 data 残留, 非 disabled) → 复用 channel 重建 account
        print(f"[recover] {cfg['name']}: channel 存在但无任何账号, 重建 account")
        channel = existing_ch
    else:
        channel = None  # 下面会 create_channel

    # API-Sec-1: 加密明文 → ciphertext bytes
    from security.crypto import encrypt
    ciphertext = encrypt(api_key, settings.aimc_master_key)

    if channel is None:
        channel = await crud.create_channel(
            session,
            name=cfg["name"],
            provider=cfg["provider"],
            base_url=cfg["base_url"],
            priority=cfg["priority"],
        )
        print(f"[+] 创建渠道: {channel.name} (id={channel.id})")
    else:
        print(f"[recover] 复用已有 channel: {channel.name} (id={channel.id})")

    account = await crud.create_account(
        session,
        channel_id=channel.id,
        account_name=f"{cfg['key']}_01",
        api_key="",                # API-Sec-1: 空字符串占位 (NOT NULL 约束兼容, router 跳过空)
        api_key_ciphertext=ciphertext,  # API-Sec-1: 存密文
        quota_total=None,
    )
    # API-Sec-3: 写 last_rotated_at (新建 account 视为刚轮换)
    from datetime import datetime
    account.last_rotated_at = datetime.now()
    await session.commit()
    # 不打印 account.id/敏感字段（只 print 名字）
    print(f"[+] 创建账号: {account.account_name} (key 已加密)")

    for price_row in cfg["prices"]:
        await _upsert_price_row(session, channel.id, price_row)
    print(f"[+] {cfg['name']} 价格已录入 ({len(cfg['prices'])} 条)")

    await crud.create_quota(
        session,
        channel_id=channel.id,
        account_id=account.id,
        quota_type="qpm",
        limit_value=cfg["qpm"],
        reset_strategy="rolling_window",
    )
    return True


async def import_demo_data():
    """导入已配置 .env 中 api_key 的渠道"""
    await init_db()

    created = 0
    async with async_session_factory() as session:
        for cfg in CHANNELS:
            if await import_one_channel(session, cfg):
                created += 1
        await session.commit()

    print(f"\n[OK] 完成：{created} 个渠道已导入")
    if created == 0:
        print("! 没有渠道被创建 — 可能原因:")
        print("  1. .env 中所有 channel key 都没填 / 填了占位符 (sk-your-...)")
        print("  2. AIMC_MASTER_KEY 未配置 (运行 python scripts/gen_master_key.py)")
        print("  3. 同名 channel 已存在, 如需迁移老明文数据跑 scripts/migrate_encrypt_keys.py")
        for cfg in CHANNELS:
            print(f"   {cfg['env_var']}=  ({cfg['name']})")
    else:
        # V4.4: 引导多账号场景用 CLI 加额外账号
        print()
        print("[V4.4 提示] 已完成首次 init。后续增量账号请用 CLI：")
        print("  python scripts/add_account.py --channel <provider> --name <name> --api-key <key>")
        print("  python scripts/list_accounts.py        # 查看所有账号（脱敏）")
        print("  python scripts/rotate_account.py rotate --id <id> --new-key <key>  # 轮换 key")
        print("  本脚本仅供 V4.3 兼容使用，V4.4 起推荐 CLI。")


if __name__ == "__main__":
    # V4.4 启动横幅
    print("=" * 60)
    print("AIMC manual_import.py — V4.3 兼容路径")
    print("V4.4 起多账号推荐: python scripts/add_account.py --help")
    print("=" * 60)
    asyncio.run(import_demo_data())
