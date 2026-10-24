"""硅基流动 model_ids → price_snapshots 增量同步 (Phase 2a+ fix, 2026-08-13)

被两处调用:
- scripts/refresh_siliconflow_models.py (CLI 手动跑)
- intelligence/scheduler.py:_run_source_task (scheduler 自动跑)

设计:
- fetch_sf_models: 拉硅基流动 /v1/models, 0.5-2s/次
- sync_siliconflow_models_to_price_snapshots: 增量 upsert 到 siliconflow_aggregator
  channel 的 price_snapshots (input/output=NULL, 聚合平台价格不固定)
- idempotent: 重复跑只 INSERT 新 model
"""

from __future__ import annotations

import httpx
from sqlalchemy import select


SF_BASE = "https://api.siliconflow.cn/v1"
SF_CHANNEL_NAME = "siliconflow_aggregator"

# 上游 /models 仍列出、但推理已 400 (20012 "Model does not exist") 的僵尸 raw id。
# 2026-10-24 直探实证: Kev-4B 经网关 / SemIf 直连上游, 均 0.1s 返 400 (列表不作数)。
# 不跳过的话, 每日 model_list 同步会把人工清理掉的 NULL 价行原样重建,
# 渠道页「待补价」永远清不空 (官方定价页也查不到这两个模型)。
SF_RETIRED_MODEL_IDS = frozenset({"Kev-4B", "SemIf"})


async def fetch_sf_models(api_key: str) -> list[str]:
    """从硅基流动 /v1/models 拉 model_id 列表.

    Args:
        api_key: 鉴权 key (Bearer)

    Returns:
        model_id 列表, 按 API 返回顺序

    Raises:
        httpx.HTTPError: 网络/HTTP 错误
    """
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            f"{SF_BASE}/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        r.raise_for_status()
    return [m["id"] for m in r.json().get("data", [])]


async def sync_siliconflow_models_to_price_snapshots(session, model_ids: list[str]) -> int:
    """把 model_ids 增量 upsert 到 siliconflow_aggregator channel 的 price_snapshots.

    Args:
        session: SQLAlchemy AsyncSession (caller 管理 commit)
        model_ids: 硅基流动 model_id 列表

    Returns:
        新增条数 (已存在的不算)

    Raises:
        RuntimeError: siliconflow_aggregator channel 不存在
    """
    from db.models import Channel, PriceSnapshot

    ch = (await session.execute(
        select(Channel).where(Channel.name == SF_CHANNEL_NAME)
    )).scalars().first()
    if ch is None:
        raise RuntimeError(
            f"{SF_CHANNEL_NAME} channel 不存在, 先跑 scripts/manual_import.py"
        )

    existing = set((await session.execute(
        select(PriceSnapshot.model).where(PriceSnapshot.channel_id == ch.id)
    )).scalars().all())

    to_add = [m for m in model_ids
              if m not in existing and m not in SF_RETIRED_MODEL_IDS]
    for model in to_add:
        session.add(PriceSnapshot(
            channel_id=ch.id,
            model=model,
            price_type="normal",
            time_range="全天",
            input_price=None,
            output_price=None,
            unit="元/百万tokens",
            source="api:siliconflow",
        ))

    if to_add:
        await session.commit()
    return len(to_add)