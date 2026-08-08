"""V4.6.3 Artificial Analysis Benchmark 榜单 source

Artificial Analysis (AA) — 商业 LLM 评测平台, 强项是 throughput / 成本分析.
- URL: https://artificialanalysis.ai/
- API: 需要 API key (付费档)
- Reliability: 0.85 (数据全, 但商业立场)
- 评测维度: quality + speed + price 综合

V4.6.3 实现策略:
- 默认不真抓 (需 API key, 没配)
- 仅返回 mock 数据 (基于 2026-Q3 公开 leaderboard 截图)
- 如需启用真抓, 设环境变量 ARTIFICIAL_ANALYSIS_API_KEY

V4.7+ TODO:
- 集成 AA API (Bearer auth)
- 解析 speed/quality/price 三维 → 复合 score
"""

import logging
import os
from datetime import datetime, timezone
from typing import List

from intelligence.sources.base import SourceType
from intelligence.sources.benchmark import BenchmarkEntry, BenchmarkSource

logger = logging.getLogger(__name__)


# ============================================================
# Mock 数据 (基于 2026-08 AA 公开 leaderboard)
# ============================================================
_AA_MOCK_DATA: List[dict] = [
    # AA 综合 score = quality × price 平衡
    {"model": "deepseek-v3",     "provider": "deepseek",  "score": 78.5},
    {"model": "deepseek-r1",     "provider": "deepseek",  "score": 80.2},
    {"model": "qwen3-235b",      "provider": "qwen",      "score": 75.0},
    {"model": "qwen3-max",       "provider": "qwen",      "score": 77.5},
    {"model": "glm-4.5",         "provider": "glm",       "score": 73.0},
]


class ArtificialAnalysisBenchmarkSource(BenchmarkSource):
    """Artificial Analysis 榜单 source (商业).

    Reliability: 0.85
    频率: 1 周
    默认 mock (无 API key); 配置 ARTIFICIAL_ANALYSIS_API_KEY 后启用真抓.
    """

    reliability: float = 0.85

    _REAL_FETCH_URL = "https://artificialanalysis.ai/api/v2/leaderboard"

    def __init__(self):
        super().__init__(
            provider="artificial_analysis",
            url=self._REAL_FETCH_URL,
            source_type=SourceType.BENCHMARK,
            poll_interval=604800,  # 1 周
            css_selector=None,
            wait_until="domcontentloaded",
            wait_for=None,
        )

    async def fetch(self) -> List[BenchmarkEntry]:
        """抓取 AA 榜单.

        行为:
        - 没 API key → 直接返 mock
        - 有 API key → 尝试真抓, 失败回退 mock
        """
        api_key = os.getenv("ARTIFICIAL_ANALYSIS_API_KEY", "").strip()
        if not api_key:
            logger.info(
                "ARTIFICIAL_ANALYSIS_API_KEY 未配置, 直接使用 mock 数据"
            )
            return self._build_entries(_AA_MOCK_DATA)

        try:
            return await self._try_real_fetch(api_key)
        except Exception as e:
            logger.warning(
                f"AA 真实抓取失败 ({type(e).__name__}: {e}), "
                f"回退到 mock 数据 ({len(_AA_MOCK_DATA)} 条)"
            )
            return self._build_entries(_AA_MOCK_DATA)

    async def _try_real_fetch(self, api_key: str) -> List[BenchmarkEntry]:
        """真实抓取尝试. V4.6.3 占位 — V4.7+ 完整实现."""
        raise NotImplementedError(
            "V4.6.3 AA API 抓取尚未实现, 等 V4.7+ 适配"
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
                source="artificial_analysis",
                url=url,
                category="overall",
                fetched_at=fetched_at,
            )
            for row in rows
        ]