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
        "prices": [
            ("deepseek-chat", "normal", "全天", 1.0, 2.0),
            ("deepseek-chat", "offpeak", "00:00-08:00", 0.5, 1.0),
            ("deepseek-reasoner", "normal", "全天", 4.0, 16.0),
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
        "prices": [
            ("moonshot-v1-128k", "normal", "全天", 6.0, 6.0),
            ("moonshot-v1-32k", "normal", "全天", 3.0, 3.0),
        ],
    },
    {
        # Phase 2D-3: MiniMax 渠道 (2026-07-24)
        # OpenAI-compatible 代理 — base_url + model 名称需用户填 (留 placeholder)
        # 用 MINIMAX_BASE_URL + MINIMAX_MODEL 单独配（其他渠道用硬编码 base_url）
        "key": "minimax",
        "name": "minimax_compat",
        "provider": "minimax",
        "base_url": os.getenv("MINIMAX_BASE_URL", "https://PLACEHOLDER.minimax/v1"),
        "priority": 5,
        "env_var": "MINIMAX_API_KEY",
        "qpm": 60,
        "prices": [
            # 价格 placeholder — 等用户填真实计费后改
            ("gpt-4o-mini", "normal", "全天", 0.0, 0.0),
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
]


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
        # 检查该 channel 是否有 active account
        existing_acc = (await session.execute(
            select(AccountModel).where(
                AccountModel.channel_id == existing_ch.id,
                AccountModel.status == "active",
            )
        )).scalars().first()
        if existing_acc is not None:
            print(f"[skip] {cfg['name']}: channel + account 已存在 (channel_id={existing_ch.id}), 如需更新 key 走 dashboard")
            return False
        # channel 在但没 active account (老 data 残留) → 复用 channel 重建 account
        print(f"[recover] {cfg['name']}: channel 存在但无 active account, 重建 account")
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

    for model, price_type, time_range, in_p, out_p in cfg["prices"]:
        await crud.create_price_snapshot(
            session,
            channel_id=channel.id,
            model=model,
            price_type=price_type,
            time_range=time_range,
            input_price=in_p,
            output_price=out_p,
            source="manual",
        )
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


if __name__ == "__main__":
    asyncio.run(import_demo_data())
