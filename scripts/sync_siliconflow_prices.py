"""阶段B (2026-08-15; 2026-09-09 全面校准): 同步硅基流动真实 input/output 价格 + cache 价估算

背景:
- 之前 (P0-3 follow-up) 只 hardcode 9 行 cache 价, SF 渠道 price_snapshots 的
  input_price / output_price 一直是 NULL (路由 cheapest 策略用 999 兜底)
- 从 https://www.siliconflow.cn/pricing (RSC HTML) 解析出 model 的真实
  input/output 价格 (元/百万 tokens)
- 2026-09-09 对照官方 pricing 页全面复核 97 行快照, 修正多处错价/串价
  (如 V4-Flash 挂了 Kimi 的价、Ling-flash 挂了 Ling-mini 的价、
  MiniMax-M2.5 输出价 1.2 实为 8.4 等), 并补齐 GLM-5.3 / V3.1-Terminus /
  MiniMax-M2.5 / Nex-N2-Pro / Step-3.5-Flash / Ling-mini 等 NULL 行
- cache 命中价 SF 网页/API 不提供, 用 input × 0.1 估算 (SF 公开 cache 约 10% off)
  - 免费 model (input=0) cache 也 0

实现:
1. SF_PRICES: raw_id -> (input_price, output_price) 真实价
   - 值为 (None, None) 表示清除该行价格 (按张/按条计费的图像/语音/视频模型
     或 token 价不适用/未核实的行, 置 NULL 回到路由 999 兜底)
2. 对每个 SF price_snapshot:
   - 匹配 SF_PRICES -> 写 input_price / output_price ((None, None) 则清 NULL)
   - cache_price = input * 0.1 (免费则 0)
   - 无匹配 -> 跳过 (保留 NULL)
3. dry-run by default, --apply 才写
4. 只 UPDATE 现有行, 不 INSERT; 分段/阶梯价取非优惠基准段写全天行
   (DeepSeek-V4-Flash 3/9, Hunyuan-A13B 1/4, Qwen3.5 阶梯取 [0,128k) 段)

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
# 数据来源: https://www.siliconflow.cn/pricing
#   2026-08-15 首次解析; 2026-09-09 对照官方页全面复核 (97 行快照)
# raw_id -> (input_price, output_price); (None, None) = 清除价格
# 分段价说明: V4-Flash 高峰段 3/9 (凌晨 1.5/4.5)、Hunyuan-A13B 高峰 1/4 (平峰 0.8/3.2),
#   取非优惠基准段; Qwen3.5-122B/27B/35B/397B 为阶梯价, 取 [0,128k) 段
SF_PRICES = {
    'BAAI/bge-large-en-v1.5': (0, 0),
    'BAAI/bge-large-zh-v1.5': (0, 0),
    'BAAI/bge-m3': (0, 0),
    'BAAI/bge-reranker-v2-m3': (0, 0),
    'PaddlePaddle/PaddleOCR-VL-1.5': (0, 0),
    'Pro/BAAI/bge-m3': (0.07, 0.07),
    'Pro/BAAI/bge-reranker-v2-m3': (0.07, 0),
    'Pro/MiniMaxAI/MiniMax-M2.5': (2.1, 8.4),
    'Pro/deepseek-ai/DeepSeek-R1': (2, 8),
    'Pro/deepseek-ai/DeepSeek-V3.1-Terminus': (4, 12),
    'Qwen/Qwen2.5-14B-Instruct': (0.7, 0.7),
    'Qwen/Qwen2.5-32B-Instruct': (1.26, 1.26),
    'Qwen/Qwen2.5-72B-Instruct-128K': (4.13, 4.13),
    'Qwen/Qwen2.5-7B-Instruct': (0, 0),
    'Qwen/Qwen3-14B': (0.5, 2),
    'Qwen/Qwen3-32B': (1, 4),
    'Qwen/Qwen3-8B': (0, 0),
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
    # 官方页未见独立 token 价 (疑串价), 清 NULL 回 999 兜底
    'Qwen/Qwen3-VL-Reranker-8B': (None, None),
    'Qwen/Qwen3.5-122B-A10B': (0.8, 6.4),
    'Qwen/Qwen3.5-27B': (0.6, 4.8),
    'Qwen/Qwen3.5-35B-A3B': (0.4, 3.2),
    'Qwen/Qwen3.5-397B-A17B': (1.2, 7.2),
    # 旧值 1.6/12.8 未经官方页核实, 清 NULL 回 999 兜底
    'Qwen/Qwen3.5-4B': (None, None),
    'Qwen/Qwen3.6-27B': (3, 18),
    'Qwen/Qwen3.6-35B-A3B': (1.8, 10.8),
    'THUDM/GLM-4-32B-0414': (1.89, 1.89),
    'THUDM/GLM-4-9B-0414': (4.13, 4.13),
    'THUDM/GLM-Z1-9B-0414': (0, 0),
    'baidu/ERNIE-Image-Turbo': (None, None),
    'deepseek-ai/DeepSeek-OCR': (0, 0),
    'deepseek-ai/DeepSeek-R1': (2, 8),
    'deepseek-ai/DeepSeek-R1-0528-Qwen3-8B': (0.14, 0),
    'deepseek-ai/DeepSeek-V3': (0, 0),
    'deepseek-ai/DeepSeek-V3.2': (4, 6),
    'deepseek-ai/DeepSeek-V3.1-Terminus': (4, 12),
    'deepseek-ai/DeepSeek-V4-Flash': (3, 9),
    'deepseek-ai/DeepSeek-V4-Pro': (12, 24),
    'inclusionAI/Ling-flash-2.0': (1, 4),
    'inclusionAI/Ling-mini-2.0': (0.5, 2),
    'meituan-longcat/LongCat-2.0': (5, 20),
    'moonshotai/Kimi-K2.7-Code': (6.5, 27),
    'nex-agi/Nex-N2-Pro': (1.75, 7),
    'stepfun-ai/Step-3.5-Flash': (0.7, 2.1),
    'tencent/Hunyuan-A13B-Instruct': (1, 4),
    'tencent/Hunyuan-MT-7B': (0, 0),
    # 按张计费的图像模型, token 价不适用 -> 清 NULL
    'Tongyi-MAI/Z-Image': (None, None),
    'MiniMaxAI/MiniMax-M2.5': (2.1, 8.4),
    'zai-org/GLM-4.5-Air': (1, 6),
    'zai-org/GLM-4.5V': (1, 6),
    'zai-org/GLM-5.2': (8, 28),
    'zai-org/GLM-5.3': (8, 28),
}

# cache 价估算比例 (SF cache 命中约 10% off, 免费 model 保持 0)
CACHE_RATIO = 0.1


def find_sf_prices(model_raw_id: str):
    """按 raw_id (大小写不敏感) 找 (input_price, output_price)

    Returns:
        (input_price, output_price) 或 (None, None) 未匹配
        命中表内 (None, None) 值表示清除该行价格
    """
    if not model_raw_id:
        return None, None
    lower = model_raw_id.lower()
    for key, prices in SF_PRICES.items():
        if key.lower() == lower:
            return prices
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
            (ip, op) = find_sf_prices(p.raw_model_id or p.model)
            if ip is None and (p.raw_model_id or p.model).lower() not in {
                k.lower() for k in SF_PRICES
            }:
                skipped += 1
                continue
            cache_price = round(ip * CACHE_RATIO, 4) if ip else 0.0
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
