"""Hunyuan (腾讯混元) 采集源

Phase 4 v4.3.1 (2026-08-13): HunyuanAnnouncementSource 优惠情报公告采集
"""

from intelligence.sources.base import IntelSourceBase


class HunyuanAnnouncementSource(IntelSourceBase):
    """腾讯混元公告页采集

    URL: https://cloud.tencent.com/announce/product/AI
    抓取策略: domcontentloaded (官方公告站 SSR)
    频率: 30 分钟

    Phase 4 v4.3.1: 补齐 7 家未接厂商
    注: Hunyuan 没有免费, 靠充值. 公告主要为 "新模型上线" / "活动开放",
        不一定有强优惠, 但用跟其他厂商同一 source_type 统一管理.
    """

    def __init__(self):
        super().__init__(
            provider="hunyuan",
            url="https://cloud.tencent.com/announce/product/AI",
            source_type="announcement",
            poll_interval=1800,
            css_selector="main",
            wait_until="domcontentloaded",
            wait_for="main",
        )