"""采集源基类

定义插件式采集源的接口协议。
每个厂商实现一个采集源，注册到调度器。
"""

from dataclasses import dataclass, field
from typing import Optional


# Phase 3.1 (2026-07-29) — 采集源类型常量
# 统一在一个地方, 避免硬编码字符串拼写错误
class SourceType:
    """采集源类型枚举 (字符串常量, 用于 DB IntelSource.source_type)

    演进历史:
    - Phase 2A: web_page / api_doc / announcement (价格/公告)
    - Phase 3.1: + model_list / price_aggregator / benchmark (模型/榜单)

    语义:
    - web_page:        厂商官方文档站/定价页 (默认)
    - api_doc:         API 文档页 (e.g. 某个 endpoint 描述)
    - announcement:    公告页 (新模型/新功能发布)
    - model_list:      模型列表页 (发现新 model_id, 用于 model_registry)
    - price_aggregator: 第三方聚合平台价格页 (e.g. 硅基流动/火山方舟)
    - benchmark:       Benchmark 榜单 (e.g. LMSYS Arena / SuperCLUE)
    """
    WEB_PAGE = "web_page"
    API_DOC = "api_doc"
    ANNOUNCEMENT = "announcement"
    MODEL_LIST = "model_list"
    PRICE_AGGREGATOR = "price_aggregator"
    BENCHMARK = "benchmark"

    @classmethod
    def all(cls) -> list[str]:
        return [cls.WEB_PAGE, cls.API_DOC, cls.ANNOUNCEMENT,
                cls.MODEL_LIST, cls.PRICE_AGGREGATOR, cls.BENCHMARK]


# Phase 3.1: 按 source_type 推荐轮询间隔 (秒)
# 调度器会用这个表决定每个采集源的频率
# V4.8.3: 温和降频 — web_page 15min→30min, announcement 30min→1h, price_aggregator 6h→12h
DEFAULT_POLL_INTERVALS = {
    SourceType.WEB_PAGE: 1800,           # 30 分钟 (价格页变动中等)
    SourceType.API_DOC: 3600,            # 1 小时 (API 文档变动少)
    SourceType.ANNOUNCEMENT: 3600,       # 1 小时 (公告变动频繁, 但配合自适应降频)
    SourceType.MODEL_LIST: 86400,        # 1 天 (新模型不频繁)
    SourceType.PRICE_AGGREGATOR: 43200,  # 12 小时 (价格变动中等)
    SourceType.BENCHMARK: 604800,        # 1 周 (榜单变动少)
}


@dataclass
class IntelSourceBase:
    """采集源基类

    每个厂商的采集源继承此类，定义采集目标 URL 和配置。
    """

    # 厂商标识
    provider: str = ""
    # 采集源 ID（数据库中的 intel_sources.id）
    source_id: int = 0
    # 采集目标 URL
    url: str = ""
    # 来源类型: web_page / api_doc / announcement
    source_type: str = "web_page"
    # 轮询间隔（秒）
    poll_interval: int = 1800
    # 上次内容哈希
    last_hash: Optional[str] = None
    # 上次完整内容（截断 50KB，用于相似度计算，避免 99% 一样的页面触发 AI 解析）
    last_content: Optional[str] = None
    # 连续错误计数
    error_count: int = 0
    # CSS 选择器（可选，用于精确定位内容区域）
    css_selector: Optional[str] = None
    # === 等待策略（Phase 2B-3，借鉴 crawl4ai wait_for）===
    # wait_until: load / domcontentloaded / networkidle
    #  - domcontentloaded: 最快，DOM 解析完即可（适合静态页）
    #  - load: 等所有资源加载完（适中）
    #  - networkidle: 等网络空闲 500ms（最慢，但避免抓到不完整内容）
    wait_until: str = "domcontentloaded"
    # wait_for: 可选 CSS selector，page.goto 后等该元素出现
    # 例如 ".announcement" / "main h1" / "#price-table"
    # None = 不等具体元素（仅用 wait_until 判定）
    wait_for: Optional[str] = None
    # wait_for_timeout: 等 wait_for 的超时（毫秒），默认 10s
    wait_for_timeout: int = 10000
    # 是否启用
    enabled: bool = True
    # === Phase 3.6 (2026-08-06): 结构化抽取 (API) ===
    # api_endpoint: 如设置, scheduler 优先用 /v1/models API 结构化抽取 (fetch_models_via_api)
    #              不设置则走 HTML 抓取 + AI 解析路径 (向后兼容)
    # 示例: "https://api.siliconflow.cn/v1"
    api_endpoint: Optional[str] = None
    # api_key_from: 从 channels 表读 api_key 的 provider 标识 (e.g. "siliconflow" / "deepseek")
    #              None 表示该 endpoint 无需鉴权 (公开 list)
    api_key_from: Optional[str] = None
