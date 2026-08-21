"""修复 minimax_compat 渠道 price_snapshots.model 字段大小写不一致
(配合 450499c 命名归一化, 同步 price_snapshots 表)

背景:
- 450499c commit 把 model_id 字段全部小写归一化 (e.g. minimax-m3)
- 但 manual_import.py (脚本 1d011e2 才修) 的 minimax_compat cfg 仍用大写 (MiniMax-M3)
- 期间跑的部署在 price_snapshots 表里留了 8 个大写行
- router 用 fuzzy 匹配部分工作, 但 minimax-m3 (multimodal) fuzzy 失败, 触发 503
- 本脚本把 minimax_compat 渠道的 8 个大写行改成小写, 跟 model_registry 对齐

用法:
    python scripts/normalize_minimax_prices.py            # dry-run
    python scripts/normalize_minimax_prices.py --apply   # 写 db
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from db.engine import async_session_factory
from db.init_db import init_db
from db.models import PriceSnapshot
from sqlalchemy import select, update

# minimax_compat 渠道的 model_id 大小写映射
MAPPING = {
    "MiniMax-M3": "minimax-m3",
    "MiniMax-M2.7": "minimax-m2.7",
    "MiniMax-M2.7-highspeed": "minimax-m2.7-highspeed",
    "MiniMax-M2.5": "minimax-m2.5",
    "MiniMax-M2.5-highspeed": "minimax-m2.5-highspeed",
    "MiniMax-M2.1": "minimax-m2.1",
    "MiniMax-M2.1-highspeed": "minimax-m2.1-highspeed",
    "MiniMax-M2": "minimax-m2",
}


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    await init_db()
    async with async_session_factory() as s:
        # 查实际存在的
        ch = (await s.execute(
            select(PriceSnapshot.channel_id)
            .where(PriceSnapshot.model.in_(list(MAPPING.keys())))
            .distinct()
        )).scalars().all()
        if not ch:
            print("[OK] db 里没有大写行, 无需修复")
            return
        print(f"=== 影响渠道 (channel_id): {ch} ===")

        for old, new in MAPPING.items():
            r = await s.execute(
                select(PriceSnapshot)
                .where(PriceSnapshot.model == old)
            )
            rows = r.scalars().all()
            if rows:
                print(f"  {old} -> {new}: {len(rows)} 行")
                for row in rows:
                    print(f"    channel_id={row.channel_id}")
            else:
                print(f"  {old}: (db 里没有)")

        if not args.apply:
            print("\n[DRY-RUN] 加 --apply 才写 db")
            return

        # 写 db
        for old, new in MAPPING.items():
            r = await s.execute(
                update(PriceSnapshot)
                .where(PriceSnapshot.model == old)
                .values(model=new)
            )
            if r.rowcount:
                print(f"  [UPD] {old} -> {new} ({r.rowcount} rows)")
        await s.commit()
        print("\n[OK] done")


if __name__ == "__main__":
    asyncio.run(main())
