"""启用、禁用、轮换和过期扫描 AIMC 账号。"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings
from db import crud
from db.engine import async_session_factory
from db.init_db import init_db
from scripts.expire_accounts import expire_accounts
from security.crypto import encrypt


async def rotate(args: argparse.Namespace) -> int:
    await init_db()
    if args.command == "expire-check":
        count = await expire_accounts()
        print(f"已标记 {count} 个过期账号")
        return count
    async with async_session_factory() as session:
        if args.command in {"enable", "disable"}:
            status = "active" if args.command == "enable" else "disabled"
            account = await crud.update_account_status(session, args.id, status)
        else:
            if not settings.aimc_master_key:
                raise RuntimeError("AIMC_MASTER_KEY 未配置，无法加密 API key")
            account = await crud.rotate_account_key(
                session, args.id, encrypt(args.new_key, settings.aimc_master_key)
            )
        if not account:
            raise ValueError(f"账号不存在: {args.id}")
        await session.commit()
        print(f"账号已更新: id={account.id}, status={account.status}")
        return account.id


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="管理 AIMC 账号")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("enable", "disable"):
        command = sub.add_parser(name)
        command.add_argument("--id", type=int, required=True)
    command = sub.add_parser("rotate")
    command.add_argument("--id", type=int, required=True)
    command.add_argument("--new-key", required=True)
    sub.add_parser("expire-check")
    return parser


if __name__ == "__main__":
    asyncio.run(rotate(build_parser().parse_args()))
