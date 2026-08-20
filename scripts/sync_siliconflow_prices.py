"""阶段B (2026-08-15): 同步硅基流动 53 个 model 的真实 input/output 价格 + cache 价估算

背景:
- 之前 (P0-3 follow-up) 只 hardcode 9 行 cache 价, SF 渠道 price_snapshots 的
  input_price / output_price 一直是 NULL (路由 cheapest 策略用 999 兜底)
- 从 https://www.siliconflow.cn/pricing (RSC HTML) 解析出 53 个 model 的真实
  input/output 价格 (元/百万 tokens)
- cache 命中价 SF 网页/API 不提供, 用 input × 0.1 估算 (SF 公开 cache 约 10% off)
  - 免费 model (input=0) cache 也 0

实现:
1. SF_PRICES: raw_id -> (input_price, output_price) 真实价 (53 个)
2. 对每个 SF price_snapshot:
   - 匹配 SF_PRICES -> 写 input_price / output_price
   - cache_price = input * 0.1 (免费则 0)
   - 无匹配 -> 跳过 (保留 NULL)
3. dry-run by default, --apply 才写

用法:
    python scripts/sync_siliconflow_prices.py            # dry-run
    python scripts/sync_siliconflow_prices.py --apply   # 实际写入
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


# 硅基流动真实 input/output 价 (元/百万 tokens)
# 数据来源: https://www.siliconflow.cn/pricing (2026-08-15 RSC HTML 解析)
# raw_id -> (input_price, output_price)
SF_PRICES = {
    'BAAI/bge-large-en-v1.5': (0, 0),
    'BAAI/bge-large-zh-v1.5': (0.07, 0.07),
    'BAAI/bge-m3': (0, 0),
    'BAAI/bge-reranker-v2-m3': (0, 0),
    'PaddlePaddle/PaddleOCR-VL-1.5': (2.1, 8.4),
    'Pro/BAAI/bge-m3': (0.35, 0.35),
    'Pro/BAAI/bge-reranker-v2-m3': (0.07, 0),
    'Pro/MiniMaxAI/MiniMax-M2.5': (2, 1.2),
    'Pro/deepseek-ai/DeepSeek-R1': (2, 8),
    'Pro/deepseek-ai/DeepSeek-V3.1-Terminus': (4, 16),
    'Qwen/Qwen2.5-14B-Instruct': (0.7, 0.7),
    'Qwen/Qwen2.5-32B-Instruct': (1.26, 1.26),
    'Qwen/Qwen2.5-72B-Instruct-128K': (4.13, 4.13),
    'Qwen/Qwen2.5-7B-Instruct': (0, 0),
    'Qwen/Qwen3-14B': (0.5, 2),
    'Qwen/Qwen3-32B': (1, 4),
    'Qwen/Qwen3-8B': (2, 3),
    'Qwen/Qwen3-Coder-30B-A3B-Instruct': (1, 6),
    'Qwen/Qwen3-Embedding-4B': (4, 16),
    'Qwen/Qwen3-Omni-30B-A3B-Captioner': (0.7, 2.8),
    'Qwen/Qwen3-Omni-30B-A3B-Thinking': (0.7, 2.8),
    'Qwen/Qwen3-Reranker-0.6B': (0.07, 0),
    'Qwen/Qwen3-Reranker-4B': (0.07, 0),
    'Qwen/Qwen3-VL-30B-A3B-Instruct': (0.5, 5),
    'Qwen/Qwen3-VL-30B-A3B-Thinking': (0.7, 2.8),
    'Qwen/Qwen3-VL-32B-Instruct': (1, 4),
    'Qwen/Qwen3-VL-32B-Thinking': (1, 10),
    'Qwen/Qwen3-VL-8B-Instruct': (0.5, 2),
    'Qwen/Qwen3-VL-Embedding-8B': (0.7, 0),
    'Qwen/Qwen3-VL-Reranker-8B': (2.1, 8.4),
    'Qwen/Qwen3.5-27B': (0.7, 0),
    'Qwen/Qwen3.5-35B-A3B': (0.7, 2.1),
    'Qwen/Qwen3.5-397B-A17B': (2, 16),
    'Qwen/Qwen3.5-4B': (1.6, 12.8),
    'Qwen/Qwen3.6-35B-A3B': (1.6, 12.8),
    'THUDM/GLM-4-32B-0414': (0, 0),
    'THUDM/GLM-4-9B-0414': (4.13, 4.13),
    'THUDM/GLM-Z1-9B-0414': (0, 0),
    'Tongyi-MAI/Z-Image': (1, 2),
    'baidu/ERNIE-Image-Turbo': (6, 28),
    'deepseek-ai/DeepSeek-OCR': (0, 0),
    'deepseek-ai/DeepSeek-R1': (2, 8),
    'deepseek-ai/DeepSeek-R1-0528-Qwen3-8B': (0.14, 0),
    'deepseek-ai/DeepSeek-V3': (0, 0),
    'deepseek-ai/DeepSeek-V3.2': (2, 3),
    'deepseek-ai/DeepSeek-V4-Flash': (6.5, 27),
    'deepseek-ai/DeepSeek-V4-Pro': (12, 24),
    'inclusionAI/Ling-flash-2.0': (0.5, 2),
    'meituan-longcat/LongCat-2.0': (5, 20),
    'moonshotai/Kimi-K2.7-Code': (1.75, 7),
    'tencent/Hunyuan-A13B-Instruct': (0.7, 2.8),
    'zai-org/GLM-4.5-Air': (1, 6),
    'zai-org/GLM-4.5V': (1.5, 4),
    'zai-org/GLM-5.2': (8, 28),
}

# cache 价估算比例 (SF cache 命中约 10% off, 免费 model 保持 0)
CACHE_RATIO = 0.1


def find_sf_prices(model_raw_id: str):
    """按 raw_id (大小写不敏感) 找 (input_price, output_price)

    Returns:
        (input_price, output_price) 或 (None, None) 未匹配
    """
    if not model_raw_id:
        return None, None
    lower = model_raw_id.lower()
    for key, (ip, op) in SF_PRICES.items():
        if key.lower() == lower:
            return ip, op
    return None, None


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    await init_db()

    async with async_session_factory() as s:
        sf = (await s.execute(
            select(Channel).where(Channel.name == "siliconflow_aggregator")
        )).scalars().first()
        if not sf:
            print("[ERR] siliconflow_aggregator channel 不存在")
            return

        print(f"=== SiliconFlow channel id={sf.id} ({len(SF_PRICES)} 个 model 价格) ===")

        prices = (await s.execute(
            select(PriceSnapshot).where(PriceSnapshot.channel_id == sf.id)
        )).scalars().all()
        print(f"硅基流动现有 {len(prices)} 行 price_snapshot")

        updates = []
        skipped = 0
        for p in prices:
            ip, op = find_sf_prices(p.raw_model_id or p.model)
            if ip is None:
                skipped += 1
                continue
            cache_price = round(ip * CACHE_RATIO, 4) if ip > 0 else 0.0
            if (p.input_price, p.output_price, p.cached_input_price) == (ip, op, cache_price):
                skipped += 1
                continue
            updates.append((p.id, p.model, ip, op, cache_price))

        print(f"\n=== 计划更新 {len(updates)} 行 ===")
        for pid, model, ip, op, cip in updates[:12]:
            print(f"  [{pid}] {model[:50]}: input={ip} output={op} cache={cip}")
        if len(updates) > 12:
            print(f"  ... 还有 {len(updates) - 12} 行")
        print(f"\n跳过 {skipped} 行 (无匹配价格或已是最新)")

        if not args.apply:
            print("\n[DRY-RUN] 加 --apply 才写 db")
            return

        for pid, _, ip, op, cip in updates:
            await s.execute(
                update(PriceSnapshot)
                .where(PriceSnapshot.id == pid)
                .values(
                    input_price=ip,
                    output_price=op,
                    cached_input_price=cip,
                )
            )
        await s.commit()
        print(f"\n[OK] 更新 {len(updates)} 行 price_snapshots (input/output/cache)")


if __name__ == "__main__":
    asyncio.run(main())
