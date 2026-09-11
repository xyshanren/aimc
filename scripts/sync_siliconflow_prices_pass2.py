#!/usr/bin/env python3
"""硅基流动价格核对第二批（pass 2）。

依据 https://www.siliconflow.cn/pricing 官方页面（含展开区域）与生产库
price_snapshots(channel_id=3, effective_to IS NULL) 全量比对后的修正清单。

规则：
- 只列 chat/completions 可达的按 token 计价模型；生图(/张)、音频(/千字符)、
  视频(/个)、LoRA 定制不在此列，保持 NULL。
- 时段/长度分档模型（DeepSeek-V4-Flash 2-8点、Hunyuan-A13B 9-18点、
  Qwen3.5 系列 128k+、GLM-5.1 32k+）schema 无法表达分档，统一取标准/上限档。
- UPDATE-only，按行 id 精确更新，绝不 INSERT/DELETE。

用法: cd ~/aimc && .venv/bin/python scripts/sync_siliconflow_prices_pass2.py [--apply]
默认 dry-run。
"""
import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select

from db.engine import async_session_factory
from db.models import PriceSnapshot

# (price_snapshots.id, 期望 input, 期望 output)  单位: 元/百万 tokens
# output 0.0 表示该模型仅按输入计价（官方页面输出列为 "-"）。
FIXES = {
    # --- 生产值与官方不符 ---
    50: (4.0, 16.0),   # deepseek-ai/DeepSeek-R1: 库 2/8 → 官方 4/16
    51: (4.0, 16.0),   # Pro/DeepSeek-R1: 库 2/8 → 官方 4/16
    52: (2.0, 8.0),    # deepseek-ai/DeepSeek-V3: 库 0/0(误标免费) → 官方 2/8
    59: (0.7, 2.8),    # Qwen3-VL-30B-A3B-Instruct: 库 0.5/5.0 → 官方 0.7/2.8
    77: (0.7, 2.8),    # Qwen3-Coder-30B-A3B-Instruct: 库 1.0/6.0 → 官方 0.7/2.8
    86: (0.14, 0.0),   # Qwen3-Reranker-4B: 库 0.07 → 官方 0.14 (仅输入)
    87: (0.14, 0.0),   # Qwen3-Embedding-4B: 库 4.0/16.0(chat价误挂) → 官方 0.14
    80: (0.0, 0.0),    # DeepSeek-R1-0528-Qwen3-8B: 库 0.14 → 官方免费
    92: (0.0, 0.0),    # THUDM/GLM-4-9B-0414: 库 4.13 → 官方免费
    # --- 生产 NULL，官方有价 ---
    53: (2.0, 8.0),    # Pro/DeepSeek-V3
    36: (4.0, 6.0),    # Pro/DeepSeek-V3.2
    27: (6.5, 27.0),   # Pro/moonshotai/Kimi-K2.6
    28: (8.0, 28.0),   # Pro/zai-org/GLM-5.1 (32k+ 上限档, 分档取保守)
    107: (0.35, 0.35),  # Pro/Qwen2.5-7B-Instruct
    100: (4.13, 4.13),  # Qwen2.5-72B-Instruct
    78: (0.7, 2.8),    # Qwen3-30B-A3B-Instruct-2507
    61: (0.7, 2.8),    # Qwen3-Omni-30B-A3B-Instruct
    58: (0.5, 5.0),    # Qwen3-VL-8B-Thinking
    48: (0.7, 0.0),    # Qwen3-VL-Reranker-8B (仅输入)
    45: (0.5, 4.0),    # Qwen3.5-9B ([0,128k) 档)
    46: (0.0, 0.0),    # Qwen3.5-4B 官方免费
    71: (1.5, 4.0),    # ByteDance-Seed/Seed-OSS-36B-Instruct
    84: (0.28, 0.0),   # Qwen3-Reranker-8B (仅输入)
    85: (0.28, 0.0),   # Qwen3-Embedding-8B (仅输入)
    89: (0.07, 0.0),   # Qwen3-Embedding-0.6B (仅输入)
}

# 官方免费且库已为 0/0，确认无需改动（核对记录用）
CONFIRMED_FREE = {
    91,   # THUDM/GLM-Z1-9B-0414
    83,   # Qwen/Qwen3-8B
    103,  # Qwen/Qwen2.5-7B-Instruct
    49,   # PaddlePaddle/PaddleOCR-VL-1.5
    64,   # deepseek-ai/DeepSeek-OCR
    70,   # tencent/Hunyuan-MT-7B
    96, 97, 104, 105,  # BAAI bge 系列
}


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="实际写库")
    args = parser.parse_args()

    async with async_session_factory() as session:
        result = await session.execute(
            select(PriceSnapshot).where(PriceSnapshot.id.in_(FIXES.keys()))
        )
        rows = {r.id: r for r in result.scalars()}
        for pid, (inp, out) in sorted(FIXES.items()):
            row = rows.get(pid)
            if row is None:
                print(f"[SKIP] id={pid} 不存在")
                continue
            cur_in = float(row.input_price) if row.input_price is not None else None
            cur_out = float(row.output_price) if row.output_price is not None else None
            if cur_in == inp and cur_out == out:
                print(f"[SAME] id={pid} {row.raw_model_id or row.model} 已是 {inp}/{out}")
                continue
            print(
                f"[{'OK' if args.apply else 'PLAN'}] id={pid} "
                f"{row.raw_model_id or row.model}: "
                f"{cur_in}/{cur_out} -> {inp}/{out}"
            )
            if args.apply:
                row.input_price = inp
                row.output_price = out
        if args.apply:
            await session.commit()
            print("\n[OK] 已提交")
        else:
            print(f"\n[dry-run] 待更新 {len(FIXES)} 行, 加 --apply 生效")


if __name__ == "__main__":
    asyncio.run(main())
