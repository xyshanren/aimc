"""SenseNova (商汤日日新) 采集源 - 文档站专项

V6.14 (2026-09-27): SenseNovaDocsSource — 官方文档站"支持的模型"监测 (用户拍板)

2026-09-27 调查 (生产 Playwright 实测):
- https://platform.sensenova.cn/docs 渲染后 ~78KB, 含模型总览/各模型 API 文档
  (U1.5 Lite / U1.5 Fast / DeepSeek V4.1 Flash / GLM-5.2 / Kimi K3 / 6.8 Flash Lite)。
  侧栏即"当前支持的模型"清单 — 上下架时侧栏变化 → hash 变化 → AI 解析 → 情报项。
- 旧结论 (2026-09-09, "登录墙内 SPA 渲染 0 字符") 已过时: 文档站改版后免登录可采。
  坑: fetch 必须等 SPA 渲染完 — domcontentloaded+1.5s 只拿到 278 字符侧栏骨架
  (过不了 content_guard 500 字符线), 必须 networkidle + 12s。
- 同日实证: /docs 侧栏与用户口径一致 (U1.5 Fast/Lite 上架, 6.7-flash-lite 与
  DeepSeek V4 Pro 下架), 比上游 /models API 更新更快 (u1.5-fast/v4-pro 尚残留在
  /models 列表)。

注: SenseNovaModelListSource (API /models) 在 model_lists_extra.py,
    SenseNovaAnnouncementSource (token-plan 页) 在 sensenova_announcement.py,
    SenseNovaPricingSource (token-plan 定价) 在 price_aggregator.py
"""

from intelligence.sources.base import IntelSourceBase


class SenseNovaDocsSource(IntelSourceBase):
    """商汤日日新官方文档站采集 (支持的模型监测)"""

    def __init__(self):
        super().__init__(
            provider="sensenova",
            url="https://platform.sensenova.cn/docs",
            source_type="web_page",
            poll_interval=1800,
            css_selector=None,
            wait_until="networkidle",
            wait_for=None,
            wait_for_timeout=12000,
        )
