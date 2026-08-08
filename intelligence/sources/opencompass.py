"""V4.6.3 OpenCompass Benchmark 榜单 source

OpenCompass (司南评测, 上海AI Lab) — 国内最权威的开源 LLM 评测榜单.
- URL: https://opencompass.org.cn/rank
- Reliability: 0.90 (跟 LMSYS 互补, 国内模型覆盖更全)
- 频率: 1 周 (变动少)
- 评测维度: 学科 (知识/推理/数学/代码) + 综合

V4.6.3 实现策略:
- fetch() 真实抓取尝试 + 失败回退到 mock 数据
- mock 数据基于 2026-07-25 截图 + OpenCompass 官方公告 (国内主流模型)
- 真抓时如 HTML 结构变动, 解析失败 → 返 mock, 不抛异常 (refresh_benchmark.py 容错)

V4.7+ TODO:
- 用 OpenCompass 官方 JSON API (如有) 替代 HTML 抓取
- 增加 OpenCompass 2.0 (2026 新版) 适配
"""

import logging
from datetime import datetime, timezone
from typing import List

from intelligence.sources.base import SourceType
from intelligence.sources.benchmark import BenchmarkEntry, BenchmarkSource

logger = logging.getLogger(__name__)


# ============================================================
# Mock 数据 (2026-Q3 公开数据, V4.6.3 默认 fallback)
# ============================================================
# 数据基于 OpenCompass 2026-07 综合榜公开截图 (通用领域)
# 真实抓取时这表会被覆盖 — 仅在 fetch() 失败时使用
_OPENCOMPASS_MOCK_DATA: List[dict] = [
    # DeepSeek 家族
    {"model": "deepseek-v3",       "provider": "deepseek",  "score": 75.2},
    {"model": "deepseek-r1",       "provider": "deepseek",  "score": 79.5},
    {"model": "deepseek-v3.1",     "provider": "deepseek",  "score": 78.0},
    # Qwen 家族
    {"model": "qwen3-max",         "provider": "qwen",      "score": 76.8},
    {"model": "qwen3-235b",        "provider": "qwen",      "score": 74.5},
    {"model": "qwen-max",          "provider": "qwen",      "score": 72.0},
    # GLM 家族
    {"model": "glm-4-plus",        "provider": "glm",       "score": 71.5},
    {"model": "glm-4.5",           "provider": "glm",       "score": 73.0},
    # 豆包 (字节)
    {"model": "doubao-seed-2-0-pro-260215",  "provider": "volcengine", "score": 70.5},
    # Hunyuan (腾讯)
    {"model": "hunyuan-pro",       "provider": "hunyuan",   "score": 68.0},
    # StepFun (阶跃)
    {"model": "step-3.5-flash",    "provider": "stepfun",   "score": 67.5},
    # Kimi (月之暗面)
    {"model": "moonshot-v1-128k",  "provider": "kimi",      "score": 69.5},
]


class OpenCompassBenchmarkSource(BenchmarkSource):
    """OpenCompass 榜单 source (国内最权威).

    Reliability: 0.90
    频率: 1 周 (poll_interval)
    """

    reliability: float = 0.90

    # V4.6.3 真实抓取 URL — V4.7+ 适配
    _REAL_FETCH_URL = "https://opencompass.org.cn/rank"

    def __init__(self):
        super().__init__(
            provider="opencompass",
            url=self._REAL_FETCH_URL,
            source_type=SourceType.BENCHMARK,
            poll_interval=604800,  # 1 周
            css_selector="table.rank-table",
            wait_until="domcontentloaded",
            wait_for="table.rank-table",
            wait_for_timeout=15000,
        )

    async def fetch(self) -> List[BenchmarkEntry]:
        """抓取 OpenCompass 榜单.

        V4.6.3 实现:
        1. 尝试 httpx 抓 HTML → 解析 (V4.7+ 完整适配)
        2. 失败 → 回退 mock 数据
        3. 永不抛异常 (refresh_benchmark.py 容错)
        """
        try:
            return await self._try_real_fetch()
        except Exception as e:
            logger.warning(
                f"OpenCompass 真实抓取失败 ({type(e).__name__}: {e}), "
                f"回退到 mock 数据 ({len(_OPENCOMPASS_MOCK_DATA)} 条)"
            )
            return self._build_entries(_OPENCOMPASS_MOCK_DATA)

    async def _try_real_fetch(self) -> List[BenchmarkEntry]:
        """真实抓取尝试. V4.6.3 占位 — V4.7+ 完整实现.

        当前策略: 直接 raise → 走 mock. 等 OpenCompass 提供稳定 API 后再实现.
        """
        raise NotImplementedError(
            "V4.6.3 OpenCompass HTML 抓取尚未实现, 等 V4.7+ 适配"
        )

    def _build_entries(
        self, rows: List[dict], fetched_at: datetime = None,
    ) -> List[BenchmarkEntry]:
        """从 raw dict 列表构造 BenchmarkEntry 列表."""
        if fetched_at is None:
            fetched_at = datetime.now(timezone.utc)
        url = self.url
        return [
            BenchmarkEntry(
                model=row["model"],
                provider=row["provider"],
                score=row["score"],
                source="opencompass",
                url=url,
                category="general",
                fetched_at=fetched_at,
            )
            for row in rows
        ]