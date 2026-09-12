"""V4.6.3 批量刷新 benchmark 数据脚本

调用所有 BenchmarkSource.fetch() → benchmark_aggregator.aggregate() → 写入 model_cards 表.

用法:
    python scripts/refresh_benchmark.py

行为:
- 启动时打印 banner + 配置 (master_key / DB url)
- 遍历 5 个 source (LMSYS / OpenCompass / LiveBench / AA / SuperCLUE)
- 每个 source.fetch() 容错 (失败不影响其他)
- 聚合 → 写入 model_cards (按 (model, benchmark='aggregated') upsert)
- 打印 [summary] 报告

演进:
- V4.6.3 (2026-08-08): 首次实现
"""

import asyncio
import sys
from pathlib import Path

# 将项目根目录加入 sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()

from db import crud  # noqa: E402
from db.engine import async_session_factory  # noqa: E402
from db.init_db import init_db  # noqa: E402
from intelligence.benchmark_aggregator import aggregate, to_model_cards_rows  # noqa: E402
from intelligence.sources.artificial_analysis import ArtificialAnalysisBenchmarkSource  # noqa: E402
from intelligence.sources.benchmark import BenchmarkEntry  # noqa: E402
from intelligence.sources.livebench import LiveBenchBenchmarkSource  # noqa: E402
from intelligence.sources.opencompass import OpenCompassBenchmarkSource  # noqa: E402
from intelligence.sources.benchmark import LMSYSArenaBenchmarkSource, SuperCLUEBenchmarkSource  # noqa: E402

import logging

logger = logging.getLogger(__name__)


# ============================================================
# 5 个 source (Phase 3.1 + V4.6.3 三个新 source)
# ============================================================
def _all_sources() -> list:
    """返回所有 benchmark source 实例."""
    return [
        LMSYSArenaBenchmarkSource(),       # stub, 返空
        OpenCompassBenchmarkSource(),      # 真实 mock fallback
        SuperCLUEBenchmarkSource(),        # stub, 返空
        LiveBenchBenchmarkSource(),        # 真实 mock fallback
        ArtificialAnalysisBenchmarkSource(),  # API key gate, 没 key 走 mock
    ]


async def refresh_all_sources() -> list[BenchmarkEntry]:
    """遍历所有 source.fetch(), 容错 (单失败不影响其他)."""
    all_entries = []
    print("\n=== 抓取各 source ===")
    for source in _all_sources():
        try:
            entries = await source.fetch()
            print(
                f"  [{source.provider:25s}] {len(entries):3d} 个 entry "
                f"(reliability={source.reliability})"
            )
            all_entries.extend(entries)
        except Exception as e:
            print(f"  [{source.provider:25s}] FAILED: {type(e).__name__}: {e}")
            # 不抛, 继续下一个 source
    return all_entries


async def write_to_model_cards(aggregates, session) -> dict:
    """聚合结果写入 model_cards 表.

    策略 (upsert 简化版):
    - 查 (model, benchmark='aggregated') 是否存在
    - 存在 → update score / updated_at / notes
    - 不存在 → insert 新 row

    Returns:
        {"created": N, "updated": M, "skipped": K}
    """
    from db.models import ModelCard
    from sqlalchemy import select

    result = {"created": 0, "updated": 0, "skipped": 0}
    rows = to_model_cards_rows(aggregates)

    for row in rows:
        # 查 provider — 从 row 拿不到, 用聚合的第一条 entry 的 provider
        # 简化: 留空 (现有 ModelCard 不强制 provider 匹配)
        model = row["model"]
        # 找现有 row
        existing = (await session.execute(
            select(ModelCard).where(
                ModelCard.model == model,
                ModelCard.benchmark == "aggregated",
            )
        )).scalars().first()

        if existing is not None:
            existing.score = row["score"]
            existing.notes = row["notes"]
            # updated_at 由 onupdate=datetime.now 自动刷新
            result["updated"] += 1
        else:
            session.add(ModelCard(
                model=model,
                provider=row.get("provider", "") or "",
                benchmark="aggregated",
                score=row["score"],
                source_url=row.get("source_url"),
                notes=row["notes"],
            ))
            result["created"] += 1

    return result


async def run_refresh():
    """主入口."""
    print("=" * 60)
    print("AIMC refresh_benchmark.py — V4.6.3 多源 benchmark 刷新")
    print("=" * 60)

    await init_db()

    # 1) 抓所有 source
    entries = await refresh_all_sources()
    print(f"\n=== 聚合: {len(entries)} 个 entry ===")

    if not entries:
        print("\n! 所有 source 都返回空 (LMSYS/SuperCLUE 仍是 stub)")
        print("  V4.6.3 仅 OpenCompass/LiveBench/AA 返 mock 数据")
        print("  等 V4.7+ 适配真抓后, 数据会自然增加")
        return

    # 2) 聚合
    aggregates = aggregate(entries)
    print(f"  融合结果: {len(aggregates)} 个 model")

    # 3) 打印 top-5
    top5 = sorted(
        aggregates.values(), key=lambda a: a.composite_score, reverse=True,
    )[:5]
    print("\n  Top-5:")
    for agg in top5:
        sources = ", ".join(c["source"] for c in agg.contributing_sources)
        print(
            f"    {agg.model:30s} {agg.composite_score:6.2f}  "
            f"({agg.sample_size} sources: {sources})"
        )

    # 4) 写入 model_cards
    print("\n=== 写入 model_cards ===")
    async with async_session_factory() as session:
        write_result = await write_to_model_cards(aggregates, session)
        await session.commit()
        print(
            f"  [summary] created={write_result['created']}, "
            f"updated={write_result['updated']}, "
            f"skipped={write_result['skipped']}"
        )

    print("\n=" * 60)
    print("[OK] V4.6.3 benchmark 刷新完成")
    print("     下一步: 重启 gateway 让 RouteEngine 重新加载 model_cards 缓存")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(run_refresh())