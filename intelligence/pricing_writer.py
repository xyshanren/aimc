"""Phase 4 v4.3 (2026-08-13): 价格快照写入器

把 AI 解析出的 pricing 列表（ParseResult.models[].pricing）增量 upsert 到 price_snapshots 表。
支持 3 种 price_type:
- normal: 常规按量计费
- cache_hit: 上下文缓存命中
- free: 完全免费

设计:
- 每个 (channel, model, price_type, time_range) 唯一 upsert
- 改价时刷新 router cache (跟 Phase 3.4 sync_price_change 一致)
- id 缺失 (channel 不存在) 时跳过, 不污染
"""

from __future__ import annotations

import logging
from typing import Iterable, Optional

from db import crud
from db.engine import async_session_factory
from db.models import Channel, PriceSnapshot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from schemas.intel import ModelIntelItem, ModelPricingItem, ParseResult

logger = logging.getLogger(__name__)


async def write_pricing_snapshots_from_parse(
    parsed: ParseResult,
    provider: str,
) -> tuple[int, int]:
    """把 ParseResult.models[].pricing 列表写入 price_snapshots.

    Args:
        parsed: AI 解析结果 (intelligence_kind=pricing)
        provider: 厂商标识 (跟 channel.provider 对应)

    Returns:
        (synced_count, refreshed_count)
        - synced_count: upsert 价格条数
        - refreshed_count: 触发 router cache invalidation 的次数
    """
    if not parsed.models:
        return 0, 0

    async with async_session_factory() as session:
        # 1. 查 provider 对应的 active channel
        ch = await crud.get_channel_by_provider(session, provider)
        if ch is None:
            logger.warning(
                "pricing_writer: provider=%s 没有 active channel, skip (先跑 manual_import.py)",
                provider,
            )
            return 0, 0

        synced = 0
        refreshed = 0
        for item in parsed.models:
            if not item.pricing:
                continue
            n, r = await _upsert_pricing_for_model(session, ch.id, item)
            synced += n
            refreshed += r

        if synced:
            await session.commit()
            logger.info(
                "pricing_writer 同步: provider=%s channel=%s models=%d synced=%d refreshed=%d",
                provider, ch.name, len(parsed.models), synced, refreshed,
            )

        return synced, refreshed


async def _upsert_pricing_for_model(
    session: AsyncSession,
    channel_id: int,
    item: ModelIntelItem,
) -> tuple[int, int]:
    """单个 model 的所有 pricing 项 upsert 到 price_snapshots.

    Returns:
        (synced_count, refreshed_count)
    """
    synced = 0
    refreshed = 0
    for pricing in item.pricing:
        try:
            change = await crud.upsert_price_snapshot(
                session,
                channel_id=channel_id,
                model=item.model_id,
                input_price=pricing.input_price,
                output_price=pricing.output_price,
                source="crawler",
                price_type=pricing.type,
                time_range=pricing.time_range or "全天",
            )
        except Exception as e:
            logger.warning(
                "pricing_writer upsert 失败: channel_id=%s model=%s type=%s err=%s",
                channel_id, item.model_id, pricing.type, e,
            )
            continue

        synced += 1

        # 价格变动 → 刷 router cache (跟 Phase 3.4 sync_price_change 行为对齐)
        if change is not None and getattr(change, "changed", False):
            try:
                from intelligence.pricing_sync import apply_price_change_to_route_cache
                if await apply_price_change_to_route_cache(change):
                    refreshed += 1
            except Exception as e:
                logger.warning(
                    "pricing_writer 刷 router cache 失败 (非阻断): model=%s err=%s",
                    item.model_id, e,
                )

    return synced, refreshed