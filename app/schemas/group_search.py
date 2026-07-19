from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class GroupSearchRequest(BaseModel):
    account_id: str
    keyword: str = Field(min_length=1, max_length=100)


class AutoQueueRequest(BaseModel):
    """Auto-enqueue qualifying search results into the Smart Join Queue."""
    account_id: str
    keyword: str | None = None
    min_members: int = Field(default=50, ge=0)


class AutoQueueResponse(BaseModel):
    queued: int
    skipped_already_joined: int
    skipped_already_queued: int
    skipped_below_threshold: int
    skipped_no_username: int


class PublicGroupInfo(BaseModel):
    """Result from Telegram search returned to frontend for user selection."""
    chat_id: str
    title: str
    chat_type: str | None = None
    username: str | None = None
    participants_count: int | None = None
    about: str | None = None


class GroupSearchResultRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    account_id: str
    keyword: str
    chat_id: str
    title: str
    chat_type: str | None
    username: str | None
    participants_count: int | None
    about: str | None
    is_joined: bool
    created_at: datetime


class GroupSearchResultExtended(GroupSearchResultRead):
    """Extended result with join eligibility info."""
    can_join: bool = True
    cannot_join_reason: str | None = None


class JoinGroupRequest(BaseModel):
    result_ids: list[str] = Field(min_length=1, max_length=50, description="IDs of GroupSearchResult rows to join")


class JoinGroupV2Request(BaseModel):
    account_id: str
    result_ids: list[str] = Field(min_length=1, max_length=50, description="IDs of GroupSearchResult rows to join")


class GroupJoinLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    account_id: str
    chat_id: str
    title: str
    username: str | None
    keyword: str
    success: bool
    error_message: str | None
    created_at: datetime


class GroupJoinLogExtended(GroupJoinLogRead):
    """Join log with retry information."""
    can_retry: bool = False
    retry_action: str | None = None


class JoinInfo(BaseModel):
    """Status of daily join limit."""
    joined_today: int
    max_daily: int
    remaining: int


class GroupJoinStats(BaseModel):
    """Aggregated join statistics for an account."""
    total_attempts: int
    successful_joins: int
    failed_joins: int
    success_rate: float
    today_remaining: int
    max_daily: int


class GroupSearchResultList(BaseModel):
    """Paginated search results."""
    items: list[GroupSearchResultRead]
    total: int
    keyword: str


class GroupJoinLogList(BaseModel):
    """Paginated join logs."""
    items: list[GroupJoinLogRead]
    total: int
    page: int
    page_size: int
    total_pages: int
