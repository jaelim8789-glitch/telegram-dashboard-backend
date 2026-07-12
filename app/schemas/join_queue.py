"""Pydantic schemas for the Smart Join Queue API."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


# ── Queue Item Schemas ───────────────────────────────────────────────────────


class QueueItemRead(BaseModel):
    id: str
    account_id: str
    raw_link: str
    title: Optional[str] = None
    chat_type: Optional[str] = None
    username: Optional[str] = None
    chat_id: Optional[str] = None
    status: str
    error_message: Optional[str] = None
    flood_wait_until: Optional[datetime] = None
    position: int
    delay_before_seconds: Optional[float] = None
    created_at: datetime
    updated_at: datetime
    processed_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class QueueItemCreate(BaseModel):
    """One link to add to the queue, echoed from a prior /inspect response."""
    raw_link: str = Field(..., description="t.me link, invite link, or @username")
    title: str = ""
    chat_type: Optional[str] = None
    username: Optional[str] = None
    chat_id: Optional[str] = None
    delay_before_seconds: Optional[float] = None


class AddToQueueRequest(BaseModel):
    account_id: str
    items: list[QueueItemCreate] = Field(min_length=1, max_length=200)


class AddToQueueResponse(BaseModel):
    items: list[QueueItemRead]
    total_added: int


class ListQueueRequest(BaseModel):
    account_id: str
    status: Optional[str] = None
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=50, ge=1, le=200)


class PaginatedQueueItems(BaseModel):
    items: list[QueueItemRead]
    total: int
    page: int
    page_size: int
    total_pages: int


class RemoveFromQueueRequest(BaseModel):
    item_ids: list[str] = Field(min_length=1, max_length=100)


class RemoveFromQueueResponse(BaseModel):
    removed_count: int


class ClearQueueRequest(BaseModel):
    account_id: str
    status: Optional[str] = None


class ClearQueueResponse(BaseModel):
    cleared_count: int


# ── Queue Config Schemas ─────────────────────────────────────────────────────


class QueueConfigRead(BaseModel):
    account_id: str
    is_paused: bool
    joins_per_hour: int
    max_daily_joins: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class QueueConfigUpdate(BaseModel):
    is_paused: Optional[bool] = None
    joins_per_hour: Optional[int] = Field(default=None, ge=1, le=60)
    max_daily_joins: Optional[int] = Field(default=None, ge=1, le=100)


# ── Queue Status / Stats ─────────────────────────────────────────────────────


class QueueStats(BaseModel):
    account_id: str
    total_queued: int
    total_processing: int
    total_success: int
    total_failed: int
    total_flood_wait: int
    joined_today: int
    max_daily_joins: int
    is_paused: bool
    joins_per_hour: int