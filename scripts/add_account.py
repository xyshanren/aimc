"""添加一个加密账号到 AIMC 数据库。"""

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings
from db import crud
from db.engine import async_session_factory
from db.init_db import init_db
from db.models import Channel
from security.crypto import encrypt
from sqlalchemy import select


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="添加 AIMC 账号")
    parser.add_argument("--channel", required=True, help="已有渠道 provider，例如 deepseek")
    parser.add_argument("--name", required=True, help="账号名称")
    parser.add_argument("--api-key", required=True, help="API key，不会写入明文字段")
    parser.add_argument("--quota-total", type=int, default=None)
    parser.add_argument("--expires-at", default=None, help="ISO 日期或时间")
    parser.add_argument("--notes", default=None)
    return parser


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


async def add_account(args: argparse.Namespace) -> int:
    if not settings.aimc_master_key:
        raise RuntimeError("AIMC_MASTER_KEY 未配置，无法加密 API key")
    await init_db()
    async with async_session_factory() as session:
        channel = (await session.execute(
            select(Channel).where(Channel.provider == args.channel, Channel.status == "active").order_by(Channel.id)
        )).scalars().first()
        if not channel:
            raise ValueError(f"未找到 active channel: {args.channel}，请先运行 manual_import.py")
        existing = (await crud.list_accounts(session, provider=args.channel))
        if any(account.account_name == args.name for account, _ in existing):
            raise ValueError(f"账号已存在: {args.name}")
        account = await crud.create_account(
            session,
            channel_id=channel.id,
            account_name=args.name,
            api_key="",
            api_key_ciphertext=encrypt(args.api_key, settings.aimc_master_key),
            status="active",
            quota_total=args.quota_total,
            quota_used=0,
            last_rotated_at=datetime.now(),
            expires_at=parse_datetime(args.expires_at),
            notes=args.notes,
        )
        await session.commit()
        print(f"账号创建成功: id={account.id}, channel={channel.provider}, name={account.account_name}, status=active")
        print("API key 已加密入库；下一次路由缓存刷新后生效。")
        return account.id


if __name__ == "__main__":
    asyncio.run(add_account(build_parser().parse_args()))
