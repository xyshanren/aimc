"""StepFun (阶跃星辰) 采集源 - announcement 专项

Phase 4 v4.3.1 (2026-08-13): StepFunAnnouncementSource 优惠情报公告采集
"""

from intelligence.sources.base import IntelSourceBase


class StepFunAnnouncementSource(IntelSourceBase):
    """阶跃星辰公告页采集

    URL: https://platform.stepfun.com/news
    抓取策略: domcontentloaded
    频率: 30 分钟

    Phase 4 v4.3.1: 补齐 7 家未接厂商
    注: StepFun step-plan 页 (pricing source) 已在 price_aggregator.py,
        本 source 单独抓 news 页
    """

    def __init__(self):
        super().__init__(
            provider="stepfun",
            url="https://platform.stepfun.com/news",
            source_type="announcement",
            poll_interval=3600,  # 1小时 (V4.8.3: 原 30min→1h)
            css_selector="main",
            wait_until="domcontentloaded",
            wait_for="main",
        )