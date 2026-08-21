"""MiniMax (MiniMax) 采集源

Phase 4 v4.3.1 (2026-08-13): MiniMaxAnnouncementSource 优惠情报公告采集
"""

from intelligence.sources.base import IntelSourceBase


class MiniMaxAnnouncementSource(IntelSourceBase):
    """MiniMax 公告页采集

    URL: https://platform.minimaxi.com/announcements
    抓取策略: domcontentloaded (官方文档站 SSR)
    频率: 30 分钟

    Phase 4 v4.3.1: 补齐 7 家未接厂商
    注: MiniMaxPricingSource (token-plan 页) 已在 price_aggregator.py,
        本 source 单独抓 announcements 页
    """

    def __init__(self):
        super().__init__(
            provider="minimax",
            url="https://platform.minimaxi.com/announcements",
            source_type="announcement",
            poll_interval=1800,
            css_selector="main",
            wait_until="domcontentloaded",
            wait_for="main",
        )