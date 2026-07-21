from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class BroadcastSearchItem(BaseModel):
    id: str
    account_id: str
    message: str
    status: str
    scheduled_at: datetime | None = None
    sent_at: datetime | None = None
    created_at: datetime | None = None
    error_message: str | None = None
    retry_count: int = 0
    recipient_count: int = 0
    delivery_mode: str | None = None
    campaign_id: str | None = None
    is_recurring: bool = False
    failure_info: dict | None = None

    model_config = {"from_attributes": True}


class BroadcastSearchResponse(BaseModel):
    items: list[BroadcastSearchItem]
    total: int
    page: int
    page_size: int
    total_pages: int
