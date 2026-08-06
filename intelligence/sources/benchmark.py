"""Phase 3.1 (2026-07-29): Benchmark 榜单采集源

用途: 自动更新 model_cards.benchmark_scores 字段
      (dashboard 展示 + 路由决策参考, 不直接参与路由)
来源类型: benchmark

演进: Phase 2D-4 已经手动 seed 了一批 model_cards (43 张 + OpenCompass 引用)
      这个 source 是自动化补充, 让榜单数据保持新鲜
"""

from intelligence.sources.base import IntelSourceBase, SourceType


class LMSYSArenaBenchmarkSource(IntelSourceBase):
    """LMSYS Arena Leaderboard (聊天机器人竞技场)

    频率: 1 周 (榜单变动少)
    """

    def __init__(self):
        super().__init__(
            provider="lmsys",
            url="https://lmarena.ai/leaderboard",
            source_type=SourceType.BENCHMARK,
            poll_interval=604800,  # 1 周
            css_selector="main, table",
            wait_until="networkidle",  # SPA 多
            wait_for="table",
            wait_for_timeout=20000,
        )


class SuperCLUEBenchmarkSource(IntelSourceBase):
    """SuperCLUE 榜单 (国内大模型评测, 中文为主)

    频率: 1 周
    """

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