"""V6.5+ (2026-09-14): Free-LLM-Collection 免费 API 合集观察源

调研背景 (docs/dev/next-stage-research.md §2.6.2):
- 上游项目: https://github.com/for-the-zero/Free-LLM-Collection
  单人手工维护的免费 LLM API 静态清单 (~26 来源 / 100+ 模型,
  仅收录 OpenAI 兼容格式, 含 base_url + 速率/额度 + 推荐模型)。
- 价值 = 「新免费源出现 / 免费额度变更」是比厂商降价页更高频的
  可路由情报品类; intel 验收死线 (~2026-10-06) 前多一条产出渠道。

设计:
- source_type=PRICE_AGGREGATOR (第三方聚合语义, 与 openrouter 同类)
- URL 用 GitHub raw 直出 (纯静态 Markdown, domcontentloaded, 无需 selector)
- poll_interval=86400 (1 天; 清单更新频率低, 自适应降频会自动拉长)
- 不配 api_endpoint (纯 HTML/文本抓取 + AI 解析路径)

数据消费:
- 内容变化 → AI 解析 → 落 promotions 审批队列 (免费类按 S3 双轨拍板
  仅入库展示, 人工审批后才可能转价格行) — 不是缺陷是设计
- GitHub raw 直连若生产网络不可达 → content_guard 抓禁用 + 告警
  (既有机制), 不影响其他源
"""

from intelligence.sources.base import IntelSourceBase, SourceType


class FreeLLMCollectionSource(IntelSourceBase):
    """Free-LLM-Collection GitHub README (免费 API 合集, 观察性数据源)

    监控目标:
    - 新免费 API 源条目 (厂商/base_url/额度)
    - 已有条目的额度/限速变更 (e.g. RPD 下调、key 有效期政策变化)
    - 新推荐模型 (反映免费模型供给趋势)
    """

    def __init__(self):
        super().__init__(
            provider="free_llm_collection",
            url=(
                "https://raw.githubusercontent.com/for-the-zero/"
                "Free-LLM-Collection/main/README.md"
            ),
            source_type=SourceType.PRICE_AGGREGATOR,
            poll_interval=86400,  # 1 天
            css_selector=None,  # raw markdown 全文
            wait_until="domcontentloaded",  # 纯静态文本, 非 SPA
        )
