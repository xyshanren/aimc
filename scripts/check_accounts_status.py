"""检查 DB 中 active 账号数（V4.4 部署引导用）

供 scripts/deploy/install.sh 在 install 末尾调用：
- 返回 active 账号总数（stdout 输出纯数字）
- 0 时 install.sh 引导用户跑 manual_import.py / add_account.py
- 异常时输出 err:<message>，install.sh graceful skip

用法:
    python scripts/check_accounts_status.py            # 打印 active 账号数
    python scripts/check_accounts_status.py --json     # 输出 JSON 详细
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select, func

from db.engine import async_session_factory
from db.init_db import init_db
from db.models import Account, Channel


async def count_active_accounts() -> int:
    await init_db()
    async with async_session_factory() as session:
        result = await session.execute(
            select(func.count(Account.id)).where(Account.status == "active")
        )
        return int(result.scalar() or 0)


async def summarize() -> dict:
    """返回更详细的账号分布，给 --json 用"""
    await init_db()
    out: dict = {"total_active": 0, "by_provider": {}}
    async with async_session_factory() as session:
        rows = (
            await session.execute(
                select(Account, Channel)
                .join(Channel, Account.channel_id == Channel.id)
                .where(Account.status == "active")
            )
        ).all()
        out["total_active"] = len(rows)
        for account, channel in rows:
            out["by_provider"][channel.provider] = (
                out["by_provider"].get(channel.provider, 0) + 1
            )
        return out


def main() -> int:
    parser = argparse.ArgumentParser(description="检查 AIMC 账号状态 (V4.4)")
    parser.add_argument(
        "--json",
        action="store_true",
        help="输出 JSON 详细（按 provider 分布）",
    )
    args = parser.parse_args()

    try:
        if args.json:
            data = asyncio.run(summarize())
            print(json.dumps(data, ensure_ascii=False))
        else:
            n = asyncio.run(count_active_accounts())
            print(n)
    except Exception as e:  # noqa: BLE001
        # install.sh 期望纯数字; 异常用 err: 前缀
        print(f"err:{e}", file=sys.stdout)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())