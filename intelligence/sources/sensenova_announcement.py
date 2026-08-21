"""SenseNova (商汤日日新) 采集源 - announcement 专项

Phase 4 v4.3.1 (2026-08-13): SenseNovaAnnouncementSource 优惠情报公告采集

注: SenseNovaModelListSource (API) 已在 model_lists_extra.py 中, 这里单独放 announcement.
"""

from intelligence.sources.base import IntelSourceBase


class SenseNovaAnnouncementSource(IntelSourceBase):
    """商汤日日新公告页采集

    URL: https://platform.sensenova.cn/news
    抓取策略: domcontentloaded
    频率: 30 分钟

    Phase 4 v4.3.1: 补齐 7 家未接厂商
    注: SenseNova token-plan 页 (pricing source) 已在 price_aggregator.py,
        本 source 单独抓 news 页 (优惠/活动信息)
    """

    def __init__(self):
        super().__init__(
            provider="sensenova",
            url="https://platform.sensenova.cn/news",
            source_type="announcement",
            poll_interval=1800,
            css_selector="main",
            wait_until="domcontentloaded",
            wait_for="main",
        )