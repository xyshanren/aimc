"""情报/采集相关 Pydantic Schema"""

from pydantic import BaseModel
from typing import Optional


class IntelSourceCreate(BaseModel):
    provider: str
    source_type: str  # web_page / api_doc / announcement
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
    """AI 解析结果"""
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


class ParseRuleCreate(BaseModel):
    provider: str
    url_pattern: Optional[str] = None
    css_selector: Optional[str] = None
    ai_strategy: Optional[str] = None
    confidence: Optional[float] = None
