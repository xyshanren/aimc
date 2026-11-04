"""Phase 4 v4.3 (2026-08-13): 价格快照写入器

2026-11-04 (P3): 语义已改 —— **不再直写 price_snapshots**。
intel 解析出的价格先落 `price_change_suggestions` (待确认价行建议),
人工批准后才写价行并精准刷该渠道路由缓存。原因: 价格是路由第一排序键,
错价会立刻改变路由与成本记账, 且 intel 定价页解析本就容易错行。

支持 3 种 price_type:
- normal: 常规按量计费
- cache_hit: 上下文缓存命中
- free: 完全免费

设计:
- 每个 (channel, model, price_type, time_range) 只保留 1 条 pending 建议
- 与现价相同 → 不产建议 (不打扰人)
- channel 不存在时跳过, 不污染
"""

from __future__ import annotations

import logging
from typing import Iterable, Optional

from db import crud, price_suggestions
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
    """单个 model 的 pricing 项 → 写「待确认价行建议」(P3, 2026-11-04)。

    语义变更: **不再直写 price_snapshots**。价格是路由第一排序键, intel 错价会
    立刻改变路由与成本记账, 人工来不及察觉。现在只落 `price_change_suggestions`
    (与现价相同则不产建议), 由 dashboard 批准后才写价行并刷缓存。

    保留 O1 的规范名解析: `price_snapshots.model` 是路由键, intel 的展示名/线名
    必须先解析成 canonical, 上游线名写进 raw_model_id (严格 raw id 渠道必需)。

    Returns:
        (suggested_count, 0) 第二个返回值保留签名兼容, 不再有"刷缓存"语义
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

    suggested = 0
    for pricing in item.pricing:
        try:
            row = await price_suggestions.suggest_price(
                session,
                channel_id=channel_id,
                model=canonical,
                input_price=pricing.input_price,
                output_price=pricing.output_price,
                raw_model_id=raw_model_id,
                price_type=pricing.type,
                time_range=pricing.time_range or "全天",
                source="crawler",
            )
        except Exception as e:
            logger.warning(
                "pricing_writer 写价行建议失败: channel_id=%s model=%s type=%s err=%s",
                channel_id, canonical, pricing.type, e,
            )
            continue

        if row is not None:
            suggested += 1

    return suggested, 0