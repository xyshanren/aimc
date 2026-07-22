"""手动录入渠道/账号/价格脚本

Phase 1 使用：在爬虫未就绪前，手动录入渠道信息。

设计原则：
- **不在代码里写任何 api_key**（避免 commit 泄露）
- 从环境变量读：DEEPSEEK_API_KEY / QWEN_API_KEY / GLM_API_KEY
- 没填对应 env → 跳过该渠道（不创建占位 account）

用法：
    1. 编辑 .env，至少填一个厂商的 key：
       DEEPSEEK_API_KEY=sk-...
       QWEN_API_KEY=sk-...
       GLM_API_KEY=...
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


# 渠道元数据（不含 api_key）— 厂商 URL 公开信息
CHANNELS = [
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
]


async def import_one_channel(session, cfg: dict) -> bool:
    """导入单个渠道（如果 env 有 api_key）

    Returns:
        True = 创建了 account，False = 跳过（env 缺 key）
    """
    api_key = os.getenv(cfg["env_var"], "").strip()
    if not api_key or api_key.startswith("sk-your-"):
        print(f"[skip] {cfg['name']}: {cfg['env_var']} 未配置")
        return False

    channel = await crud.create_channel(
        session,
        name=cfg["name"],
        provider=cfg["provider"],
        base_url=cfg["base_url"],
        priority=cfg["priority"],
    )
    print(f"[+] 创建渠道: {channel.name} (id={channel.id})")

    account = await crud.create_account(
        session,
        channel_id=channel.id,
        account_name=f"{cfg['key']}_01",
        api_key=api_key,
        quota_total=None,
    )
    # ⚠️ 不打印 account.id/敏感字段（只 print 名字）
    print(f"[+] 创建账号: {account.account_name}")

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
        print("⚠️  没有渠道被创建 — 请在 .env 中至少填一个 API key：")
        for cfg in CHANNELS:
            print(f"   {cfg['env_var']}=  ({cfg['name']})")


if __name__ == "__main__":
    asyncio.run(import_demo_data())
