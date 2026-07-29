"""Backfill channels 表的 provider_type / billing_mode (Phase 2.3, 2026-07-29)

Phase 2.3 给 channels 加 2 列 (provider_type / billing_mode):
- provider_type: official / third_party / aggregator / local
- billing_mode: pay_per_token / subscription / free

老 channel 用 server_default 'official' / 'pay_per_token' 兜底（ALTER ADD COLUMN 时
SQLite 必须有 DEFAULT，NOT NULL 限制）。**这个脚本专门处理特殊 case**：

- siliconflow_aggregator → provider_type=aggregator (聚合平台不是厂商官方)
- 其他 channel → 已用 default 'official' 正确，**不需要 backfill**

idempotent: 重复跑只 UPDATE 非 default 值的 channel，幂等。
已跑过 init_db + ALTER (server_default 已生效) 时也可以重复跑，siliconflow
会被正确标 aggregator，其他不变。

用法：
    python scripts/backfill_channels_meta.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select

from db.engine import async_session_factory
from db.models import Channel


# 特殊 case: channel.name → (provider_type, billing_mode)
# 大部分 channel 用 default 'official' / 'pay_per_token' 即可 (server_default 已填)
# 只有硅基流动是 aggregator
SPECIAL_CHANNELS = {
    "siliconflow_aggregator": ("aggregator", "pay_per_token"),
}


async def backfill():
    async with async_session_factory() as session:
        channels = list((await session.execute(select(Channel))).scalars().all())
        if not channels:
            print("没有 channel, 无需 backfill")
            return

        count = 0
        for ch in channels:
            # 1) 查特殊规则
            if ch.name in SPECIAL_CHANNELS:
                expected_type, expected_billing = SPECIAL_CHANNELS[ch.name]
            else:
                # 其他 channel 用 default (server_default 已经填好 'official' / 'pay_per_token')
                # 如果之前 INSERT 时显式给了 'official' (Python-side default), 也是对的
                expected_type = "official"
                expected_billing = "pay_per_token"

            # 2) 仅当当前值不等于期望时 UPDATE (idempotent)
            if ch.provider_type != expected_type or ch.billing_mode != expected_billing:
                old_type, old_billing = ch.provider_type, ch.billing_mode
                ch.provider_type = expected_type
                ch.billing_mode = expected_billing
                count += 1
                print(
                    f"  [update] {ch.name}: "
                    f"type {old_type!r} → {expected_type!r}, "
                    f"billing {old_billing!r} → {expected_billing!r}"
                )
            else:
                print(
                    f"  [skip] {ch.name}: provider_type={ch.provider_type}, "
                    f"billing_mode={ch.billing_mode} (已正确)"
                )

        if count:
            await session.commit()
            print(f"\n[OK] backfill {count} channels")
        else:
            print("\n[OK] 无需 backfill, 全部 channel metadata 已正确")

        # 验证
        print("\n=== 当前 channels metadata ===")
        rows = list((await session.execute(
            select(Channel).order_by(Channel.name)
        )).scalars().all())
        for r in rows:
            print(
                f"  {r.name:30s}  type={r.provider_type:12s}  "
                f"billing={r.billing_mode:15s}  status={r.status}  priority={r.priority}"
            )


if __name__ == "__main__":
    asyncio.run(backfill())
