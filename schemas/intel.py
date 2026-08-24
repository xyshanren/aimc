"""情报/采集相关 Pydantic Schema"""

from pydantic import BaseModel, Field
from typing import Optional


class ModelPricingItem(BaseModel):
    """Phase 4 v4.3 (2026-08-13): AI 从采集内容中提取的单个价格快照.

    价格类型:
    - normal: 常规按量计费
    - cache_hit: 上下文缓存命中 (input_price/output_price 可为 0)
    - free: 完全免费 (input_price=0, output_price=0)
    """
    type: str = "normal"  # normal / cache_hit / free
    input_price: Optional[float] = None
    output_price: Optional[float] = None
    time_range: Optional[str] = "全天"


class ModelIntelItem(BaseModel):
    """Phase 3.2: AI 从采集内容中提取的单个模型元信息。"""

    model_id: str
    family: Optional[str] = None
    display_name: Optional[str] = None
    inferred_tier: Optional[str] = None
    capabilities: list[str] = Field(default_factory=list)
    context_length: Optional[int] = None
    modality: str = "text"
    is_open_source: Optional[bool] = None
    status: str = "active"
    confidence: float = 0.0
    confidence_level: str = "low"
    tier_reason: Optional[str] = None
    # Phase 4 v4.3: 价格快照列表 (一条 model 可对应多个 price_type)
    pricing: list[ModelPricingItem] = Field(default_factory=list)
    # V4.8.6: 采集来源渠道 (provider 标识, e.g. "moonshot" / "deepseek")
    # 用于变更记录追溯 "这个模型是从哪个渠道采集到的"
    source_provider: Optional[str] = None


class IntelSourceCreate(BaseModel):
    provider: str
    source_type: str  # web_page / api_doc / announcement / model_list / price_aggregator / benchmark
    url: str
    poll_interval: int = 900


class IntelSourceResponse(BaseModel):
    id: int
    provider: str
    source_type: str
    url: str
    poll_interval: int
    status: str
    last_check_at: Optional[str] = None
    error_count: int = 0

    model_config = {"from_attributes": True}


class ParseResult(BaseModel):
    """AI 解析结果，兼容优惠情报与 Phase 3.2 模型元信息。"""
    is_promotion: bool
    confidence: float
    title: Optional[str] = None
    provider: Optional[str] = None
    discount_desc: Optional[str] = None
    content: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    url: Optional[str] = None
    summary: Optional[str] = None
    source_type: str = "web_page"
    intelligence_kind: str = "promotion"
    models: list[ModelIntelItem] = Field(default_factory=list)


class ParseRuleCreate(BaseModel):
    provider: str
    url_pattern: Optional[str] = None
    css_selector: Optional[str] = None
    ai_strategy: Optional[str] = None
    confidence: Optional[float] = None
