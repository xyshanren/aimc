"""检查 channel API key 轮换周期 (API-Sec-3, 2026-07-29)

每天扫一次 active accounts:
- last_rotated_at 距今 > AIMC_KEY_ROTATION_DAYS (默认 30) → 告警
- expires_at 已过 → 告警
- last_rotated_at 为 NULL (老数据没填) → 用 created_at 兜底

告警走 alerter.send_alert(), throttle key = "key_rotation:{channel}:{account}"
让同 channel+account 每天最多 1 次告警 (alerter 已有 60s 窗口, 这里再叠加).

挂到 scheduler: intelligence/scheduler.py 加 daily job, 也可手动跑:
    python scripts/check_key_rotation.py
"""

import asyncio
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select

from db.engine import async_session_factory
from db.models import Account, Channel

logger = logging.getLogger(__name__)


# 默认 30 天阈值, 可通过 env 覆盖
DEFAULT_ROTATION_DAYS = 30


async def check_key_rotation(days: int = DEFAULT_ROTATION_DAYS) -> int:
    """扫所有 active accounts, 找出超过 days 天没轮换的 key, 发飞书告警.

    Returns:
        告警条数
    """
    from intelligence.alerter import send_alert

    now = datetime.now()
    threshold = now - timedelta(days=days)
    alerted = 0

    async with async_session_factory() as session:
        # 查所有 active channel + active account
        stmt = (
            select(Account, Channel)
            .join(Channel, Account.channel_id == Channel.id)
            .where(Account.status == "active", Channel.status == "active")
        )
        rows = (await session.execute(stmt)).all()

        for acc, ch in rows:
            # 1) 距今多久没轮换 (last_rotated_at 优先, fallback created_at)
            ref = acc.last_rotated_at or acc.created_at
            if ref is None:
                # 极端情况: created_at 也 NULL (老 schema) → 跳过
                continue
            if ref < threshold:
                days_since = (now - ref).days
                msg = (
                    f"[AIMC key 轮换提醒] {ch.name}/{acc.account_name} "
                    f"已 {days_since} 天未轮换 (阈值 {days} 天)\n"
                    f"最后轮换: {ref.isoformat()}\n"
                    f"建议: 在厂商控制台重置 key, 改 .env 重新跑 manual_import.py"
                )
                throttle_key = f"key_rotation:{ch.name}:{acc.account_name}"
                await send_alert(msg, key=throttle_key)
                alerted += 1
                logger.info(f"key 轮换告警: {ch.name}/{acc.account_name} ({days_since} 天)")

            # 2) expires_at 已过
            if acc.expires_at and acc.expires_at < now:
                msg = (
                    f"[AIMC key 过期警告] {ch.name}/{acc.account_name} "
                    f"已过期 {acc.expires_at.isoformat()}\n"
                    f"建议: 立即更新 key, 否则请求会失败"
                )
                throttle_key = f"key_expired:{ch.name}:{acc.account_name}"
                await send_alert(msg, key=throttle_key)
                alerted += 1
                logger.warning(f"key 过期告警: {ch.name}/{acc.account_name}")

    if alerted == 0:
        logger.info(f"key 轮换检查完成, 全部在 {days} 天内")
    else:
        logger.info(f"key 轮换检查完成, {alerted} 个告警")
    return alerted


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    count = await check_key_rotation()
    print(f"完成: {count} 个告警")


if __name__ == "__main__":
    asyncio.run(main())
