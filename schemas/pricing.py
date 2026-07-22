"""价格/优惠相关 Pydantic Schema"""

from pydantic import BaseModel
from typing import Optional


class PriceSnapshotCreate(BaseModel):
    channel_id: int
    model: str
    price_type: str = "normal"  # normal / peak / offpeak / cache_hit / free
    time_range: str = "全天"
    input_price: Optional[float] = None
    output_price: Optional[float] = None
    unit: str = "元/百万tokens"
    effective_from: Optional[str] = None
    effective_to: Optional[str] = None
    source: str = "manual"


class PriceSnapshotResponse(BaseModel):
    id: int
    channel_id: int
    model: str
    price_type: str
    time_range: str
    input_price: Optional[float] = None
    output_price: Optional[float] = None
    unit: str
    effective_from: Optional[str] = None
    effective_to: Optional[str] = None
    source: Optional[str] = None

    model_config = {"from_attributes": True}


class PromotionCreate(BaseModel):
    provider: str
    title: str
    content: Optional[str] = None
    url: Optional[str] = None
    discount_desc: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    confidence: Optional[float] = None


class PromotionResponse(BaseModel):
    id: int
    provider: str
    title: str
    content: Optional[str] = None
    url: Optional[str] = None
    discount_desc: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    status: str
    confidence: Optional[float] = None

    model_config = {"from_attributes": True}
