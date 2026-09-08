"""Hunyuan (腾讯混元) 采集源

Phase 4 v4.3.1 (2026-08-13): HunyuanAnnouncementSource 优惠情报公告采集
"""

from intelligence.sources.base import IntelSourceBase


class HunyuanAnnouncementSource(IntelSourceBase):
    """腾讯混元情报页采集

    2026-09-09 调查结论: 原公告页 cloud.tencent.com/announce/product/AI 已 404 下线;
    且混元大模型平台 (docs product 1729) 将于 2026-09-30 全面停服, 迁往
    大模型服务平台 TokenHub (docs product 1823)。故采集源直接挂 TokenHub
    模型价格页 (含套餐/计费调整, 即优惠情报落点):
    https://cloud.tencent.com/document/product/1823/130055
    实测: SSR 健康, #docArticleContent 约 1.7 万字符 (2026-09-08 更新的计费规则)。

    频率: 1 小时
    """

    def __init__(self):
        super().__init__(
            provider="hunyuan",
            url="https://cloud.tencent.com/document/product/1823/130055",
            source_type="announcement",
            poll_interval=3600,  # 1小时 (V4.8.3: 原 30min→1h)
            css_selector="#docArticleContent",
            wait_until="domcontentloaded",
            wait_for="#docArticleContent",
        )