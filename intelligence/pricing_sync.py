"""Phase 3.4 (2026-07-29) — 价格联动服务

负责把 AI 解析出的价格变动同步到 price_snapshots 表，并触发路由缓存刷新。

设计要点：
- 价格变动通过 crud.upsert_price_snapshot 落库，按 (channel, model, price_type, time_range) 去重
- 仅在 input_price 或 output_price 实际变化时返回 changed=True
- changed=True 时调用 route_engine.refresh_cache 让 group 路由拿到新价格
- 失败/异常返回 changed=False，让调度器继续下一轮（不刷告警）
"""

import logging
from dataclasses import dataclass
from typing import Optional

from db import crud
from db.engine import async_session_factory

logger = logging.getLogger(__name__)


@dataclass
class PriceChange:
    """价格变动记录，供 caller 决定是否触发额外动作"""

    channel_id: int
    model: str
    old_input_price: Optional[float]
    new_input_price: Optional[float]
    old_output_price: Optional[float]
    new_output_price: Optional[float]
    source: str = "crawler"

    @property
    def changed(self) -> bool:
        return (
            (self.old_input_price or 0) != (self.new_input_price or 0)
            or (self.old_output_price or 0) != (self.new_output_price or 0)
        )

    @property
    def cheaper(self) -> bool:
        """降价检测 (输入价或输出价下降)"""
        if self.new_input_price is None or self.old_input_price is None:
            return False
        return self.new_input_price < self.old_input_price


async def sync_price_change(
    provider: str,
    model: str,
    input_price: Optional[float],
    output_price: Optional[float],
    *,
    source: str = "crawler",
    price_type: str = "normal",
    time_range: str = "全天",
) -> Optional[PriceChange]:
    """同步价格变动。

    按 provider 找到第一个 active channel 作为承载 channel（同一厂商可能有多个账号），
    走 upsert_price_snapshot。返回值:
    - None: provider 找不到 active channel
    - PriceChange: 包含 old/new 价格 (changed 属性判定是否真变动)
    """
    if input_price is None and output_price is None:
        logger.debug("价格同步跳过: %s/%s 入参为空", provider, model)
        return None

    try:
        async with async_session_factory() as session:
            channels = await crud.list_channels(session, status="active")
            channel = next(
                (ch for ch in channels if ch.provider == provider),
                None,
            )
            if not channel:
                logger.warning("价格同步跳过: provider=%s 没有 active channel", provider)
                return None

            existing = await crud.get_active_price_snapshots(
                session, model, channel_id=channel.id
            )
            old = existing[0] if existing else None
            old_input = old.input_price if old else None
            old_output = old.output_price if old else None

            await crud.upsert_price_snapshot(
                session,
                channel_id=channel.id,
                model=model,
                input_price=input_price,
                output_price=output_price,
                source=source,
                price_type=price_type,
                time_range=time_range,
            )
            await session.commit()

            change = PriceChange(
                channel_id=channel.id,
                model=model,
                old_input_price=old_input,
                new_input_price=input_price,
                old_output_price=old_output,
                new_output_price=output_price,
                source=source,
            )

            if not change.changed:
                logger.debug(
                    "价格无变动: %s/%s input=%s output=%s",
                    provider, model, input_price, output_price,
                )
                return change

            logger.info(
                "价格变动: %s/%s input %s->%s, output %s->%s (source=%s)",
                provider, model, old_input, input_price, old_output, output_price, source,
            )
            return change
    except Exception as e:
        logger.error("价格同步失败: %s/%s - %s", provider, model, e)
        return None


async def apply_price_change_to_route_cache(change: PriceChange) -> bool:
    """价格变动触发路由缓存刷新。

    路由缓存里 prices 列表来自 price_snapshots，价格变动后缓存里仍是旧值。
    refresh_cache 会重新查 DB 加载新价，让 next select 立即拿到新价。
    """
    if not change.changed:
        return False
    try:
        from gateway.router import route_engine
        await route_engine.refresh_cache()
        logger.info("路由缓存已刷新: %s/%s", change.channel_id, change.model)
        return True
    except Exception as e:
        logger.error("路由缓存刷新失败: %s - %s", change, e)
        return False
