"""列出 AIMC 用户及其 API Key (V4.8 多用户)。

用法:
    # 列出全部 user + key
    python scripts/list_user_keys.py

    # 只看某 user
    python scripts/list_user_keys.py --username zhangsan

    # 只看 active key
    python scripts/list_user_keys.py --status active
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.engine import async_session_factory
from db.init_db import init_db
from db.models import User, ApiKey
from sqlalchemy import select


async def list_user_keys(args: argparse.Namespace) -> int:
    await init_db()
    async with async_session_factory() as session:
        # 查 user (可选过滤 username)
        user_query = select(User).order_by(User.id)
        if args.username:
            user_query = user_query.where(User.username == args.username)
        users = (await session.execute(user_query)).scalars().all()

        if not users:
            print("无用户记录")
            return 0

        total_keys = 0
        for user in users:
            # 限额显示
            daily = user.daily_token_limit if user.daily_token_limit is not None else "不限"
            monthly = user.monthly_token_limit if user.monthly_token_limit is not None else "不限"
            print(f"\n{'─' * 60}")
            print(f"用户 #{user.id}: {user.username} ({user.display_name or '-'})")
            print(f"  角色: {user.role}  状态: {user.status}")
            print(f"  日限额: {daily}  月限额: {monthly}")
            if user.notes:
                print(f"  备注: {user.notes}")

            # 查该 user 的 key (可选过滤 status)
            key_query = select(ApiKey).where(ApiKey.user_id == user.id).order_by(ApiKey.id)
            if args.status:
                key_query = key_query.where(ApiKey.status == args.status)
            keys = (await session.execute(key_query)).scalars().all()

            if not keys:
                print("  (无 API Key)")
                continue

            print(f"\n  {'ID':<5} {'Prefix':<12} {'Admin':<6} {'Status':<10} {'Label':<12} {'LastUsed':<20}")
            print(f"  {'─'*5} {'─'*12} {'─'*6} {'─'*10} {'─'*12} {'─'*20}")
            for k in keys:
                admin = "是" if k.is_admin else "否"
                last_used = k.last_used_at.strftime("%Y-%m-%d %H:%M") if k.last_used_at else "-"
                label = k.label or "-"
                print(f"  {k.id:<5} {k.key_prefix:<12} {admin:<6} {k.status:<10} {label:<12} {last_used:<20}")
            total_keys += len(keys)

        print(f"\n{'═' * 60}")
        print(f"共 {len(users)} 个用户, {total_keys} 把 key")
        return len(users)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="列出 AIMC 用户 + API Key (V4.8 多用户)")
    parser.add_argument("--username", default=None, help="按用户名过滤")
    parser.add_argument("--status", default=None, choices=["active", "revoked"],
                        help="按 key 状态过滤 (active / revoked)")
    return parser


if __name__ == "__main__":
    asyncio.run(list_user_keys(build_parser().parse_args()))
