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
    """商汤日日新官方文档站采集 (支持的模型监测)

    source_type 用 model_list 而非 web_page — healer 提交门要求
    (is_promotion 且 conf>=0.8) 或 models 非空; 文档页不是优惠页,
    web_page 型解析结果 is_promotion=False → hash 永不提交, 每轮白跑解析。
    model_list 型按模型元信息提取, 侧栏模型名清单 → models 非空 → 提交。
    """

    def __init__(self):
        super().__init__(
            provider="sensenova",
            url="https://platform.sensenova.cn/docs",
            source_type="model_list",
            poll_interval=86400,  # model_list 类型下限 (test_poll_intervals 守卫)
            css_selector=None,
            wait_until="networkidle",
            # SPA 水合竞态: 只等 networkidle 偶发只渲染出 278B 侧栏骨架
            # (过不了 500 字符守卫); 等 article 元素出现 = 正文就绪 (3/3 实证 78KB)
            wait_for="article",
            wait_for_timeout=15000,
        )
