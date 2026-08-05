"""情报/采集相关 Pydantic Schema"""

from pydantic import BaseModel, Field
from typing import Optional


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
