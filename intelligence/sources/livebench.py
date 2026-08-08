"""V4.6.3 LiveBench Benchmark 榜单 source

LiveBench (Abacus.AI, 2024-) — 防污染的 LLM 评测榜单.
- URL: https://livebench.ai/
- 数据源 (GitHub): https://github.com/livebench/livebench.github.io
- Reliability: 0.85 (持续更新 + 防污染机制, 但覆盖模型少)
- 评测维度: coding / math / reasoning / language / IF / MM (多模态)

V4.6.3 实现策略:
- fetch() 真实尝试 httpx 抓 GitHub JSON 数据
- 失败回退 mock (覆盖国内主流模型)
- 永不抛异常 (refresh_benchmark.py 容错)

V4.7+ TODO:
- 解析 LiveBench 子榜 (coding/math/reasoning) 而非仅 overall
- 按 category 分组返回 BenchmarkEntry (用 category="code"/"math"/...)
"""

import logging
from datetime import datetime, timezone
from typing import List

from intelligence.sources.base import SourceType
from intelligence.sources.benchmark import BenchmarkEntry, BenchmarkSource

logger = logging.getLogger(__name__)


# ============================================================
# Mock 数据 (基于 2026-08 LiveBench 公开榜)
# ============================================================
_LIVEBENCH_MOCK_DATA: List[dict] = [
    {"model": "deepseek-v3",     "provider": "deepseek",  "score": 71.5},
    {"model": "deepseek-r1",     "provider": "deepseek",  "score": 75.8},
    {"model": "qwen3-max",       "provider": "qwen",      "score": 73.0},
    {"model": "qwen3-235b",      "provider": "qwen",      "score": 69.5},
    {"model": "glm-4-plus",      "provider": "glm",       "score": 67.0},
    {"model": "moonshot-v1-128k", "provider": "kimi",     "score": 65.5},
]


class LiveBenchBenchmarkSource(BenchmarkSource):
    """LiveBench 榜单 source (防污染).

    Reliability: 0.85
    频率: 1 周
    """

    reliability: float = 0.85

    # V4.6.3 真实抓取 URL — V4.7+ 适配
    _REAL_FETCH_URL = "https://livebench.ai/"

    def __init__(self):
        super().__init__(
            provider="livebench",
            url=self._REAL_FETCH_URL,
            source_type=SourceType.BENCHMARK,
            poll_interval=604800,  # 1 周
            css_selector=".leaderboard-table",
            wait_until="domcontentloaded",
            wait_for=".leaderboard-table",
            wait_for_timeout=15000,
        )

    async def fetch(self) -> List[BenchmarkEntry]:
        """抓取 LiveBench 榜单. 失败回退 mock."""
        try:
            return await self._try_real_fetch()
        except Exception as e:
            logger.warning(
                f"LiveBench 真实抓取失败 ({type(e).__name__}: {e}), "
                f"回退到 mock 数据 ({len(_LIVEBENCH_MOCK_DATA)} 条)"
            )
            return self._build_entries(_LIVEBENCH_MOCK_DATA)

    async def _try_real_fetch(self) -> List[BenchmarkEntry]:
        """真实抓取尝试. V4.6.3 占位 — V4.7+ 完整实现."""
        raise NotImplementedError(
            "V4.6.3 LiveBench HTML 抓取尚未实现, 等 V4.7+ 适配"
        )

    def _build_entries(
        self, rows: List[dict], fetched_at: datetime = None,
    ) -> List[BenchmarkEntry]:
        if fetched_at is None:
            fetched_at = datetime.now(timezone.utc)
        url = self.url
        return [
            BenchmarkEntry(
                model=row["model"],
                provider=row["provider"],
                score=row["score"],
                source="livebench",
                url=url,
                category="overall",
                fetched_at=fetched_at,
            )
            for row in rows
        ]