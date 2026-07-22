"""渠道/账号相关 Pydantic Schema"""

from pydantic import BaseModel
from typing import Optional


class ChannelCreate(BaseModel):
    name: str
    provider: str
    base_url: str
    priority: int = 0


class ChannelResponse(BaseModel):
    id: int
    name: str
    provider: str
    base_url: str
    status: str
    priority: int

    model_config = {"from_attributes": True}


class AccountCreate(BaseModel):
    channel_id: int
    account_name: str
    api_key: str
    quota_total: Optional[int] = None


class AccountResponse(BaseModel):
    id: int
    channel_id: int
    account_name: str
    status: str
    quota_total: Optional[int] = None
    quota_used: int = 0

    model_config = {"from_attributes": True}


class QuotaCreate(BaseModel):
    channel_id: int
    account_id: Optional[int] = None
    quota_type: str  # qpm / tpm / daily / total / concurrent
    limit_value: int
    reset_strategy: str = "rolling_window"
