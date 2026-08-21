"""Moonshot (月之暗面) 采集源

Phase 4 v4.3.1 (2026-08-13): MoonshotAnnouncementSource 优惠情报公告采集

注: MoonshotModelListSource (API) 已在 model_lists_extra.py 中, 这里单独放 announcement.
"""

from intelligence.sources.base import IntelSourceBase


class MoonshotAnnouncementSource(IntelSourceBase):
    """月之暗面 Moonshot AI 公告页采集

    URL: https://platform.moonshot.cn/news
    抓取策略: domcontentloaded
    频率: 30 分钟

    Phase 4 v4.3.1: 补齐 7 家未接厂商
    注: Moonshot 跟 Kimi 是同公司 (月之暗面), Kimi 是产品名,
        Moonshot 是公司/API 系列. news 页有 Kimi 系列 + moonshot 系列公告.
    """

    def __init__(self):
        super().__init__(
            provider="moonshot",
            url="https://platform.moonshot.cn/news",
            source_type="announcement",
            poll_interval=1800,
            css_selector="main",
            wait_until="domcontentloaded",
            wait_for="main",
        )