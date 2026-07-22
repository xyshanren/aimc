"""路由决策相关 Pydantic Schema"""

from pydantic import BaseModel
from typing import Optional


class RouteDecision(BaseModel):
    """路由决策结果"""
    channel_id: int
    channel_name: str
    provider: str
    account_id: int
    account_name: str
    model: str
    input_price: Optional[float] = None
    output_price: Optional[float] = None
    price_type: str = "normal"
    time_range: str = "全天"


class ChannelStatus(BaseModel):
    """渠道状态（Dashboard 展示用）"""
    channel_id: int
    channel_name: str
    provider: str
    status: str
    account_count: int
    active_accounts: int
    models: list[str] = []
