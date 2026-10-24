"""硅基流动 model_list 同步退役 id 跳过名单测试 (2026-10-24)

Kev-4B / SemIf: 上游 /models 仍列出但推理 400 (20012 "Model does not exist",
2026-10-24 直探实证, 列表不作数)。人工从 price_snapshots 清理其 NULL 价行后,
每日 model_list 同步 (intel_sources id=28, poll 86400s) 会按「只补缺失」逻辑
把行原样重建 → 渠道页「待补价」永远清不空。

SF_RETIRED_MODEL_IDS 跳过名单后: 退役 id 不再重建, 正常新 id 照常入库。

用真实 in-memory sqlite, 不 mock ORM。

跑法: python -m pytest tests/test_siliconflow_sync_retired_ids.py -q
"""

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


async def _make_session():
    """真实 in-memory sqlite + 全表建库"""
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from db.models import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)()


async def _seed_channel(session):
    from db.models import Channel

    session.add(Channel(
        name="siliconflow_aggregator",
        provider="siliconflow",
        base_url="https://api.siliconflow.cn/v1",
    ))
    await session.commit()


def test_retired_ids_not_resurrected():
    """退役 raw id 在 DB 无行时被同步跳过; 正常新 id 照常入库"""
    from sqlalchemy import select
    from db.models import PriceSnapshot
    from scripts._siliconflow_sync import sync_siliconflow_models_to_price_snapshots

    async def run():
        session = await _make_session()
        await _seed_channel(session)

        added = await sync_siliconflow_models_to_price_snapshots(
            session, ["Kev-4B", "SemIf", "Qwen/Qwen3.8-27B"])

        models = set((await session.execute(
            select(PriceSnapshot.model)
        )).scalars().all())
        await session.close()
        return added, models

    added, models = asyncio.run(run())
    assert added == 1, f"只应新增 Qwen3.8-27B 一行, 实际 {added}"
    assert models == {"Qwen/Qwen3.8-27B"}, f"退役 id 不应入库, 实际 {models}"


def test_retired_ids_stay_absent_across_runs():
    """模拟人工已删行的现场: 下一次同步不重建, 正常 id 二次同步幂等不重复"""
    from sqlalchemy import select, func
    from db.models import PriceSnapshot
    from scripts._siliconflow_sync import sync_siliconflow_models_to_price_snapshots

    async def run():
        session = await _make_session()
        await _seed_channel(session)

        # 第一次同步入库正常 id (退役 id 被跳过)
        first = await sync_siliconflow_models_to_price_snapshots(
            session, ["Kev-4B", "Qwen/Qwen3.8-27B"])

        # 模拟人工把正常 id 的 NULL 行也补了价 — existing 集合应让二次同步零新增
        row = (await session.execute(
            select(PriceSnapshot).where(PriceSnapshot.model == "Qwen/Qwen3.8-27B")
        )).scalars().one()
        row.input_price = 3.0
        await session.commit()

        second = await sync_siliconflow_models_to_price_snapshots(
            session, ["Kev-4B", "SemIf", "Qwen/Qwen3.8-27B"])

        total = (await session.execute(
            select(func.count(PriceSnapshot.id))
        )).scalar()
        priced = (await session.execute(
            select(PriceSnapshot.input_price).where(
                PriceSnapshot.model == "Qwen/Qwen3.8-27B")
        )).scalar()
        await session.close()
        return first, second, total, priced

    first, second, total, priced = asyncio.run(run())
    assert first == 1
    assert second == 0, "退役 id 不被重建 + 已存在行不重复"
    assert total == 1, "两次同步后仍只有 1 行"
    assert priced == 3.0, "已补价行不被同步触碰"
