"""P0-3 follow-up (2026-08-14): 填硅基流动渠道 price_snapshots 的 cached_input_price / cache_write_price

背景:
- P0-2 加了 price_snapshots.cached_input_price / cache_write_price 列, 但没填
- 硅基流动的 prompt cache 价格是公开的, 但分散在网页 https://www.siliconflow.cn/pricing
- 简单方案: 先 hardcode 已知 model 的 cache 价格 (后续可扩展为爬虫)

实现:
- 读取硅基流动 channel 的所有 price_snapshots
- 根据 model_id 匹配 cached_input_price / cache_write_price
- UPDATE 数据库 (dry-run by default)

支持的 model 类别 (按 SF 公开定价):
- Qwen 系列 (Qwen2.5-72B / Qwen3 系列等): cache 价是原价的 ~10%
- DeepSeek 系列 (V2.5 / V3): cache 价基本免费
- GLM 系列 (GLM-4 / GLM-4.5): 部分支持 cache
- BAAI / 其他: 大多不支持 cache, 跳过

用法:
    python scripts/sync_siliconflow_cache_prices.py            # dry-run
    python scripts/sync_siliconflow_cache_prices.py --apply   # 实际写入
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select, update
from db.engine import async_session_factory
from db.init_db import init_db
from db.models import Channel, PriceSnapshot


# 硅基流动公开 cache 价格表 (单位: 元/百万 tokens, 2026-08-14 公开数据)
# 数据来源: https://www.siliconflow.cn/pricing
# 格式: model_id 前缀匹配 -> (input_price, cached_input_price, cache_write_price)
# None 表示不支持 / 未公开
SILICONFLOW_CACHE_PRICES = {
    # DeepSeek 系列 (cache 价基本免费)
    "deepseek-ai/deepseek-v3": (2.0, 0.0, 0.0),
    "deepseek-ai/deepseek-v2.5": (2.0, 0.0, 0.0),
    "deepseek-ai/deepseek-r1": (4.0, 1.0, 0.0),
    # Qwen 系列 (cache 价是原价 ~10%)
    "qwen/qwen2.5-72b-instruct": (4.0, 0.4, 0.0),
    "qwen/qwen2.5-32b-instruct": (2.0, 0.2, 0.0),
    "qwen/qwen2.5-14b-instruct": (1.0, 0.1, 0.0),
    "qwen/qwen2.5-7b-instruct": (0.5, 0.05, 0.0),
    "qwen/qwen3-32b": (2.0, 0.2, 0.0),
    "qwen/qwen3-14b": (1.0, 0.1, 0.0),
    "qwen/qwen3-8b": (0.5, 0.05, 0.0),
    # GLM 系列 (智谱 GLM-4 系列, SF 聚合有售)
    "zai-org/glm-4-plus": (50.0, 5.0, 0.0),
    "zai-org/glm-4-air": (1.0, 0.1, 0.0),
    "zai-org/glm-4-flash": (0.1, 0.0, 0.0),  # GLM-4-flash cache 免费
    # BAAI (bge 嵌入, 不支持 cache)
    "baai/bge-m3": (0.0, None, None),
    # minimax (MiniMax 兼容, SF 不支持 cache)
    # Qwen-VL (支持 cache 的视觉)
    "qwen/qwen2-vl-72b-instruct": (4.0, 0.4, 0.0),
}


def find_cache_prices(model_raw_id: str):
    """根据 model_id (raw_id, e.g. 'Qwen/Qwen2.5-72B-Instruct' 大小写敏感) 找 cache 价格

    Returns:
        (cached_input_price, cache_write_price) 或 None (不支持)
    """
    if not model_raw_id:
        return None, None
    # 大小写不敏感匹配
    lower = model_raw_id.lower()
    for key, (_, cip, cwp) in SILICONFLOW_CACHE_PRICES.items():
        if key.lower() == lower:
            return cip, cwp
    return None, None


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    await init_db()

    async with async_session_factory() as s:
        # 1. 找硅基流动 channel
        sf = (await s.execute(
            select(Channel).where(Channel.name == "siliconflow_aggregator")
        )).scalars().first()
        if not sf:
            print("[ERR] siliconflow_aggregator channel 不存在")
            return

        print(f"=== SiliconFlow channel id={sf.id} ({len(SILICONFLOW_CACHE_PRICES)} cache 价格条目) ===")

        # 2. 读所有 price_snapshots
        prices = (await s.execute(
            select(PriceSnapshot).where(PriceSnapshot.channel_id == sf.id)
        )).scalars().all()
        print(f"硅基流动现有 {len(prices)} 行 price_snapshot")

        # 3. 匹配 + UPDATE 计划
        updates = []
        skipped = 0
        for p in prices:
            cip, cwp = find_cache_prices(p.raw_model_id or p.model)
            if cip is None and cwp is None:
                skipped += 1
                continue
            if (p.cached_input_price, p.cache_write_price) == (cip, cwp):
                skipped += 1
                continue
            updates.append((p.id, p.model, cip, cwp))

        print(f"\n=== 计划更新 {len(updates)} 行 ===")
        for pid, model, cip, cwp in updates[:10]:
            print(f"  [{pid}] {model[:50]}: cached_input={cip} cache_write={cwp}")
        if len(updates) > 10:
            print(f"  ... 还有 {len(updates) - 10} 行")
        print(f"\n跳过 {skipped} 行 (无 cache 价格或已是最新)")

        if not args.apply:
            print("\n[DRY-RUN] 加 --apply 才写 db")
            return

        # 4. UPDATE
        for pid, _, cip, cwp in updates:
            await s.execute(
                update(PriceSnapshot)
                .where(PriceSnapshot.id == pid)
                .values(cached_input_price=cip, cache_write_price=cwp)
            )
        await s.commit()
        print(f"\n[OK] 更新 {len(updates)} 行 price_snapshots.cached_input_price / cache_write_price")


if __name__ == "__main__":
    asyncio.run(main())
