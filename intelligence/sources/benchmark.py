"""V4.6.3 Benchmark 榜单采集源 (whichllm 启发)

设计目标:
- 多源 benchmark 整合 (LMSYS / OpenCompass / SuperCLUE / LiveBench / Artificial Analysis)
- 每个 source 实现 fetch() 返回 List[BenchmarkEntry]
- 抽象出 BenchmarkEntry 数据类 + BenchmarkSource 基类
- 多源加权融合算法在 intelligence/benchmark_aggregator.py

演进历史:
- Phase 3.1 (2026-07-29): 创建 LMSYS / SuperCLUE 2 个 stub source (预留接口, 不真抓)
- V4.5.1 (2026-08-08): 加 model_cards.score 入路由 + recency_weight 时间衰减
- V4.6.3 (2026-08-08): 重构成 BenchmarkSource 基类 + 多 source 适配器 + 融合算法

设计原则:
- fetch() 是异步的 (跟现有 fetcher.py 一致, 用 httpx + aiohttp)
- source reliability 字段 (0-1) 表示源可信度 — LMSYS 0.95, OpenCompass 0.9, LiveBench 0.85
- recency_weight 用 V4.5.1 recency.py, 半衰期 180 天
- 失败静默 — 单 source 失败不应阻塞其他 source (refresh_benchmark.py 容错)
"""

from abc import abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from intelligence.sources.base import IntelSourceBase, SourceType


@dataclass
class BenchmarkEntry:
    """单 source 对单 model 的一次评测结果.

    Attributes:
        model: 模型 ID (跟 model_cards.model 对齐, e.g. "deepseek-chat")
        provider: 厂商 (e.g. "deepseek")
        score: 0-100 分
        source: source 标识 (e.g. "lmsys", "opencompass")
        url: 数据来源链接 (可空, 用于 dashboard 跳转)
        category: 子榜单分类 (e.g. "general" / "code" / "math", None 表示 overall)
        fetched_at: 抓取时间 (UTC), 默认 now — 用于 recency_weight
    """
    model: str
    provider: str
    score: float
    source: str
    url: str = ""
    category: Optional[str] = None
    fetched_at: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "provider": self.provider,
            "score": self.score,
            "source": self.source,
            "url": self.url,
            "category": self.category,
            "fetched_at": self.fetched_at.isoformat(),
        }


class BenchmarkSource(IntelSourceBase):
    """Benchmark 榜单 source 抽象基类.

    比 IntelSourceBase 多两个字段:
    - reliability: source 可信度 (0-1), 融合时作为权重基数
    - fetch(): 异步抓取入口, 子类必须实现

    用法:
        source = OpenCompassBenchmarkSource()
        entries = await source.fetch()  # List[BenchmarkEntry]
    """

    # Source 可信度 (0-1), 融合时作为权重基数.
    # LMSYS Arena: 0.95 (UC Berkeley, 论文支撑)
    # OpenCompass: 0.90 (国内最权威)
    # LiveBench: 0.85 (持续更新, 防污染)
    # SuperCLUE: 0.80 (中文为主)
    # Artificial Analysis: 0.85 (商业, 数据全)
    reliability: float = 0.8

    @abstractmethod
    async def fetch(self) -> List[BenchmarkEntry]:
        """异步抓取榜单数据. 子类必须实现.

        Returns:
            List[BenchmarkEntry] — 通常含多个 model 的分数

        Raises:
            NotImplementedError: 子类未实现
            httpx.HTTPError: 网络错误 (refresh_benchmark.py 会捕获并继续)
        """
        raise NotImplementedError(f"{self.__class__.__name__}.fetch() must be implemented")


# ============================================================
# 已有 source (Phase 3.1 stub) — V4.6.3 保持 stub, 不真抓
# ============================================================

class LMSYSArenaBenchmarkSource(BenchmarkSource):
    """LMSYS Arena Leaderboard (聊天机器人竞技场, UC Berkeley).

    频率: 1 周 (榜单变动少)
    Reliability: 0.95 (业界最权威)
    状态: V4.6.3 仍为 stub — LMSYS 是 React SPA + Cloudflare 防护, 抓取成本高
          V4.6.3 改继承 BenchmarkSource 但 fetch() 仍 raise (需要 Playwright + stealth)
    """

    reliability: float = 0.95

    def __init__(self):
        super().__init__(
            provider="lmsys",
            url="https://lmarena.ai/leaderboard",
            source_type=SourceType.BENCHMARK,
            poll_interval=604800,  # 1 周
            css_selector="main, table",
            wait_until="networkidle",
            wait_for="table",
            wait_for_timeout=20000,
        )

    async def fetch(self) -> List[BenchmarkEntry]:
        # TODO: 用 Playwright + stealth 抓 leaderboard 表
        # V4.6.3 暂时返回空 — 真实抓取需要专门适配 (V4.7+)
        return []


class SuperCLUEBenchmarkSource(BenchmarkSource):
    """SuperCLUE 榜单 (国内大模型评测, 中文为主).

    频率: 1 周
    Reliability: 0.80 (中文场景权威, 但评测维度跟 LMSYS 有差异)
    状态: V4.6.3 仍为 stub (SuperCLUE 网站结构经常变, 抓取脆弱)
    """

    reliability: float = 0.80

    def __init__(self):
        super().__init__(
            provider="superclue",
            url="https://www.superclueai.com/",
            source_type=SourceType.BENCHMARK,
            poll_interval=604800,  # 1 周
            css_selector="main, body",
            wait_until="domcontentloaded",
            wait_for=None,
        )

    async def fetch(self) -> List[BenchmarkEntry]:
        # TODO: V4.7+ 用 Playwright 适配 SuperCLUE 网页结构
        return []