"""Kimi (月之暗面 Moonshot AI) 采集源

Phase 4 v4.3.1 (2026-08-13): KimiAnnouncementSource 优惠情报公告采集
"""

from intelligence.sources.base import IntelSourceBase


class KimiAnnouncementSource(IntelSourceBase):
    """Kimi / Moonshot AI 公告页采集

    URL: https://platform.kimi.com/docs/announcement
    抓取策略: domcontentloaded (官方文档站 SSR)
    频率: 30 分钟 (DEFAULT_POLL_INTERVALS[ANNOUNCEMENT])

    Phase 4 v4.3.1: 补齐 7 家未接厂商的 announcement source,
    让 promotions 表有真实数据 (之前 0 条)
    """

    def __init__(self):
        super().__init__(
            provider="kimi",
            url="https://platform.kimi.com/docs/announcement",
            source_type="announcement",
            poll_interval=3600,  # 1小时 (V4.8.3: 原 30min→1h)
            css_selector="main",
            wait_until="domcontentloaded",
            wait_for="main",
        )