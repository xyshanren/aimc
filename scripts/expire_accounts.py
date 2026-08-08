"""将 expires_at 已到期的账号标记为 expired。"""

import asyncio
from datetime import datetime

from db.engine import async_session_factory
from db.models import Account
from sqlalchemy import update


async def expire_accounts() -> int:
    async with async_session_factory() as session:
        result = await session.execute(
            update(Account)
            .where(Account.expires_at.is_not(None))
            .where(Account.expires_at < datetime.now())
            .where(Account.status != "expired")
            .values(status="expired")
        )
        await session.commit()
        return result.rowcount or 0


if __name__ == "__main__":
    count = asyncio.run(expire_accounts())
    print(f"已标记 {count} 个过期账号")
