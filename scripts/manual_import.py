"""手动录入渠道/账号/价格脚本

Phase 1 使用：在爬虫未就绪前，手动录入渠道信息。

用法：
    python scripts/manual_import.py
"""

import asyncio
import sys
from pathlib import Path

# 将项目根目录加入 sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from db.engine import async_session_factory
from db.init_db import init_db
from db import crud


async def import_demo_data():
    """导入示例数据（DeepSeek + Qwen）"""
    await init_db()

    async with async_session_factory() as session:
        # === DeepSeek 渠道 ===
        ds_channel = await crud.create_channel(
            session,
            name="deepseek_official",
            provider="deepseek",
            base_url="https://api.deepseek.com/v1",
            priority=10,
        )
        print(f"[+] 创建渠道: {ds_channel.name} (id={ds_channel.id})")

        ds_account = await crud.create_account(
            session,
            channel_id=ds_channel.id,
            account_name="deepseek_01",
            api_key="sk-your-deepseek-key-here",  # 替换为实际 Key
            quota_total=None,  # 按量付费，无总配额限制
        )
        print(f"[+] 创建账号: {ds_account.account_name} (id={ds_account.id})")

        # DeepSeek 价格（示例）
        await crud.create_price_snapshot(
            session,
            channel_id=ds_channel.id,
            model="deepseek-chat",
            price_type="normal",
            time_range="全天",
            input_price=1.0,
            output_price=2.0,
            source="manual",
        )
        await crud.create_price_snapshot(
            session,
            channel_id=ds_channel.id,
            model="deepseek-chat",
            price_type="offpeak",
            time_range="00:00-08:00",
            input_price=0.5,
            output_price=1.0,
            source="manual",
        )
        await crud.create_price_snapshot(
            session,
            channel_id=ds_channel.id,
            model="deepseek-reasoner",
            price_type="normal",
            time_range="全天",
            input_price=4.0,
            output_price=16.0,
            source="manual",
        )
        print("[+] DeepSeek 价格已录入")

        # DeepSeek 频限
        await crud.create_quota(
            session,
            channel_id=ds_channel.id,
            account_id=ds_account.id,
            quota_type="qpm",
            limit_value=60,
            reset_strategy="rolling_window",
        )

        # === Qwen (阿里云百炼) 渠道 ===
        qw_channel = await crud.create_channel(
            session,
            name="qwen_bailian",
            provider="qwen",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            priority=5,
        )
        print(f"[+] 创建渠道: {qw_channel.name} (id={qw_channel.id})")

        qw_account = await crud.create_account(
            session,
            channel_id=qw_channel.id,
            account_name="qwen_01",
            api_key="sk-your-qwen-key-here",  # 替换为实际 Key
            quota_total=1000000,  # 示例：100万 token 免费额度
        )
        print(f"[+] 创建账号: {qw_account.account_name} (id={qw_account.id})")

        # Qwen 价格
        await crud.create_price_snapshot(
            session,
            channel_id=qw_channel.id,
            model="qwen-max",
            price_type="normal",
            time_range="全天",
            input_price=2.0,
            output_price=6.0,
            source="manual",
        )
        await crud.create_price_snapshot(
            session,
            channel_id=qw_channel.id,
            model="qwen-plus",
            price_type="normal",
            time_range="全天",
            input_price=0.8,
            output_price=2.0,
            source="manual",
        )
        print("[+] Qwen 价格已录入")

        # Qwen 频限
        await crud.create_quota(
            session,
            channel_id=qw_channel.id,
            account_id=qw_account.id,
            quota_type="qpm",
            limit_value=30,
            reset_strategy="rolling_window",
        )

        await session.commit()
        print("\n[OK] 示例数据导入完成！")
        print("请编辑此脚本中的 api_key 为实际值后重新运行。")


if __name__ == "__main__":
    asyncio.run(import_demo_data())
