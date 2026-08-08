"""列出 AIMC 账号并脱敏显示 API key。"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import crud
from db.engine import async_session_factory
from db.init_db import init_db


def mask_key(ciphertext: bytes | None) -> str:
    return "已加密" if ciphertext else "未配置"


async def list_accounts(args: argparse.Namespace) -> int:
    await init_db()
    async with async_session_factory() as session:
        rows = await crud.list_accounts(session, provider=args.provider, status=args.status)
        print("ID\tProvider\tName\tStatus\tUsed\tTotal\tExpires\tKey")
        for account, channel in rows:
            expires = account.expires_at.isoformat() if account.expires_at else "-"
            total = account.quota_total if account.quota_total is not None else "-"
            print(f"{account.id}\t{channel.provider}\t{account.account_name}\t{account.status}\t{account.quota_used or 0}\t{total}\t{expires}\t{mask_key(account.api_key_ciphertext)}")
        return len(rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="列出 AIMC 账号")
    parser.add_argument("--provider", default=None)
    parser.add_argument("--status", default=None)
    return parser


if __name__ == "__main__":
    asyncio.run(list_accounts(build_parser().parse_args()))
