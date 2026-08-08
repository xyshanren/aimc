"""V4.6.3 多源 benchmark 加权融合算法 (whichllm 启发)

设计目标:
- 给定 List[BenchmarkEntry] (来自多个 source), 算出每个 model 的 composite score
- 权重 = source_reliability × recency_weight(fetched_at)
- composite_score = Σ (entry.score × weight) / Σ weight

输入: List[BenchmarkEntry] (每个 source 一次, 含 model/score/source/fetched_at)
输出: Dict[model_id, AggregateScore] (含 composite_score + 各 source 贡献分解)

演进:
- V4.5.1 (2026-08-08): 加 recency_weight 时间衰减 (单 source)
- V4.6.3 (2026-08-08): 加多源加权融合算法 (本文件)

为什么 not 简单平均:
- 不同 source 可信度不同 (LMSYS 0.95 > SuperCLUE 0.80)
- 新数据比旧数据重要 (recency_weight)
- 加权融合能反映这种差异

公式:
    weight = source.reliability × recency_weight(entry.fetched_at)
    composite_score = Σ (entry.score × weight) / Σ weight

边界:
- 全部 weight=0 (都过期) → composite=0.0
- 单 source → composite = entry.score (退化为单源)
- 分数范围: composite ∈ [0, 100], 但实际 50-85 是合理区间
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from intelligence.recency import DEFAULT_HALF_LIFE_DAYS, recency_weight
from intelligence.sources.benchmark import BenchmarkEntry

logger = logging.getLogger(__name__)


@dataclass
class AggregateScore:
    """多源 benchmark 融合结果.

    Attributes:
        model: 模型 ID
        composite_score: 加权融合后的分数 (0-100)
        contributing_sources: 各 source 的贡献 [{source, score, weight, contribution_pct}]
        sample_size: 贡献 entry 数 (来自几个 source)
    """
    model: str
    composite_score: float
    contributing_sources: List[dict] = field(default_factory=list)
    sample_size: int = 0

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "composite_score": round(self.composite_score, 2),
            "sample_size": self.sample_size,
            "sources": self.contributing_sources,
        }


# Default reliability weights (跟 BenchmarkSource 子类一致)
# 这里用作 aggregator fallback (entry.source 找不到 source 实例时用)
DEFAULT_SOURCE_RELIABILITY: Dict[str, float] = {
    "lmsys": 0.95,
    "opencompass": 0.90,
    "livebench": 0.85,
    "artificial_analysis": 0.85,
    "superclue": 0.80,
}


def get_source_reliability(source_name: str) -> float:
    """查 source reliability, 找不到返默认 0.7 (保守)."""
    return DEFAULT_SOURCE_RELIABILITY.get(source_name, 0.70)


def aggregate(
    entries: List[BenchmarkEntry],
    half_life_days: int = DEFAULT_HALF_LIFE_DAYS,
    now: Optional[datetime] = None,
) -> Dict[str, AggregateScore]:
    """多源 benchmark 加权融合.

    Args:
        entries: 多个 BenchmarkEntry (来自不同 source, 不同 model)
        half_life_days: recency 半衰期 (默认 180 天)
        now: 注入的"当前时间" (测试用)

    Returns:
        Dict[model_id, AggregateScore] — 同一 model 多 source 融合

    算法:
        对每个 model:
            weights = [src_reliability × recency(entry.fetched_at) for entry]
            composite = sum(s × w for s, w in zip(scores, weights)) / sum(weights)

    边界:
        - 空 entries → 空 dict
        - 单 entry → composite = entry.score (weight 退化为 1)
        - 全部过期 (weight=0) → composite = 0.0
    """
    if not entries:
        return {}

    # 按 model 分组
    by_model: Dict[str, List[BenchmarkEntry]] = {}
    for entry in entries:
        by_model.setdefault(entry.model, []).append(entry)

    results: Dict[str, AggregateScore] = {}
    for model, model_entries in by_model.items():
        agg = _aggregate_one_model(model, model_entries, half_life_days, now)
        results[model] = agg

    logger.info(
        f"benchmark_aggregator: 融合 {len(entries)} 个 entry → "
        f"{len(results)} 个 model"
    )
    return results


def _aggregate_one_model(
    model: str,
    entries: List[BenchmarkEntry],
    half_life_days: int,
    now: Optional[datetime],
) -> AggregateScore:
    """单 model 融合算法 (核心)."""
    if not entries:
        return AggregateScore(model=model, composite_score=0.0, sample_size=0)

    contributing = []
    weighted_sum = 0.0
    weight_total = 0.0

    for entry in entries:
        # 1) source reliability
        reliability = get_source_reliability(entry.source)
        # 2) recency weight (V4.5.1)
        rec_w = recency_weight(entry.fetched_at, half_life_days, now)

        weight = reliability * rec_w
        weighted_sum += entry.score * weight
        weight_total += weight

        contributing.append({
            "source": entry.source,
            "score": entry.score,
            "weight": round(weight, 4),
            "contribution_pct": None,  # 占位, 算完 percentage 后填
            "reliability": reliability,
            "recency": round(rec_w, 3),
        })

    # 计算 composite
    if weight_total == 0:
        composite = 0.0
    else:
        composite = weighted_sum / weight_total

    # 回填 contribution_pct
    if weight_total > 0:
        for c in contributing:
            c["contribution_pct"] = round(c["weight"] / weight_total * 100, 1)

    return AggregateScore(
        model=model,
        composite_score=composite,
        contributing_sources=contributing,
        sample_size=len(entries),
    )


def to_model_cards_rows(
    aggregates: Dict[str, AggregateScore],
    source_url_template: str = "",
) -> List[dict]:
    """聚合结果转 model_cards 写入格式.

    Returns:
        List[dict] — 每条形如:
            {
                "model": "deepseek-v3",
                "provider": "deepseek",  # 不在 AggregateScore 里, 由 caller 补
                "benchmark": "aggregated",
                "score": 76.5,
                "source_url": "...",
                "notes": "composite of 3 sources: opencompass, livebench, ..."
            }

    注意: provider 不在 AggregateScore 里 (Aggregation 输入 entry 含 provider 但聚合时不保存),
          caller 需要自己补 — 通常 from 第一条 entry 或外部 provider map.
    """
    rows = []
    for model, agg in aggregates.items():
        # 拼接 source 名
        source_names = [c["source"] for c in agg.contributing_sources]
        notes = (
            f"composite of {agg.sample_size} sources: {', '.join(source_names)}"
            if source_names else "aggregated"
        )
        rows.append({
            "model": model,
            "provider": "",  # caller 填
            "benchmark": "aggregated",
            "score": round(agg.composite_score, 2),
            "source_url": source_url_template,
            "notes": notes,
        })
    return rows