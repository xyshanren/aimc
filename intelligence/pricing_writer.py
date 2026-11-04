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


async def _invalidate_channel_price(channel_id: int) -> bool:
    """写价后精准刷该渠道路由缓存 (O1 小修).

    旧实现把 crud.upsert_price_snapshot 返回的 `(snapshot, created)` 元组当
    PriceChange 传, getattr(change, 'changed') 恒 False → 缓存原地不动,
    intel 改价要等最多 5 分钟全量刷新才生效。这里直接按 channel 失效,
    幂等且便宜。
    """
    try:
        from gateway.router import route_engine
        await route_engine.invalidate_price(channel_id)
        return True
    except Exception as e:
        logger.warning(
            "pricing_writer 刷 router cache 失败 (非阻断): channel_id=%s err=%s",
            channel_id, e,
        )
        return False


async def _upsert_pricing_for_model(
    session: AsyncSession,
    channel_id: int,
    item: ModelIntelItem,
) -> tuple[int, int]:
    """单个 model 的所有 pricing 项 upsert 到 price_snapshots。

    2026-11-04 (O1 小修): 写前先解析规范名, 并把上游线名一起写进 raw_model_id。
    原因: `price_snapshots.model` 是**路由键**。intel 的 model_id 可能是展示名/线名
    (`deepseek-ai/DeepSeek-V4.1-Flash`), 直接写会造出一条永远不会被 group 路由命中的
    脏 canonical 行; 而严格 raw id 渠道 (siliconflow) 又要求规范名行带 raw_model_id,
    否则 proxy 会把规范名裸发上游 (400 model not exist)。

    Returns:
        (synced_count, refreshed_count)
    """
    from intelligence.model_resolver import resolve_canonical_model

    resolution = await resolve_canonical_model(
        session, item.model_id, item.display_name,
    )
    canonical = resolution.canonical or item.model_id
    raw_model_id = None
    if resolution.resolved:
        if canonical != item.model_id:
            raw_model_id = item.model_id.strip()
    elif "/" in item.model_id:
        # 未解析 + 名字本身是线名 → 宁可不写, 也不污染 canonical 键空间
        logger.warning(
            "pricing_writer 跳过未解析的线名: model=%s (先让 model_resolver 认得它, "
            "或用看板价格表手工配价)",
            item.model_id,
        )
        return 0, 0

    synced = 0
    refreshed = 0
    wrote_any = False
    for pricing in item.pricing:
        try:
            _, _created = await crud.upsert_price_snapshot(
                session,
                channel_id=channel_id,
                model=canonical,
                input_price=pricing.input_price,
                output_price=pricing.output_price,
                source="crawler",
                price_type=pricing.type,
                time_range=pricing.time_range or "全天",
                raw_model_id=raw_model_id,
            )
        except Exception as e:
            logger.warning(
                "pricing_writer upsert 失败: channel_id=%s model=%s type=%s err=%s",
                channel_id, canonical, pricing.type, e,
            )
            continue

        synced += 1
        wrote_any = True

    # 价格落库 → 精准刷该渠道路由缓存 (幂等且便宜, 见 _invalidate_channel_price)
    if wrote_any:
        if await _invalidate_channel_price(channel_id):
            refreshed += 1

    return synced, refreshed