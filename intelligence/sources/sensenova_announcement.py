"""SenseNova (商汤日日新) 采集源 - announcement 专项

Phase 4 v4.3.1 (2026-08-13): SenseNovaAnnouncementSource 优惠情报公告采集

注: SenseNovaModelListSource (API) 已在 model_lists_extra.py 中, 这里单独放 announcement.
"""

from intelligence.sources.base import IntelSourceBase


class SenseNovaAnnouncementSource(IntelSourceBase):
    """商汤日日新 Token Plan 套餐页采集

    2026-09-09 调查: 旧 platform.sensenova.cn/news 已 404 下线; sensecore console
    文档站为登录墙内 SPA (Playwright 渲染后仍 0 字符)。官网公开的 Token Plan 页
    (www.sensenova.cn/token-plan) 是商汤套餐/折扣情报的官方落点 ("不止折扣，
    更是办公生产力能力包"), Playwright networkidle 渲染后全文 769 字符, 健康可采。
    css_selector 用 None 取全文 (页面包含套餐档位/权益文案)。

    注: SenseNovaModelListSource (API) 在 model_lists_extra.py,
        SenseNovaPricingSource (token-plan 定价) 在 price_aggregator.py
    """

    def __init__(self):
        super().__init__(
            provider="sensenova",
            url="https://www.sensenova.cn/token-plan",
            source_type="announcement",
            poll_interval=3600,  # 1小时 (V4.8.3: 原 30min→1h)
            css_selector=None,
            wait_until="networkidle",
            wait_for=None,
            wait_for_timeout=12000,
        )