from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class GroupSearchRequest(BaseModel):
    account_id: str
    keyword: str = Field(min_length=1, max_length=100)


class PublicGroupInfo(BaseModel):
    """Result from Telegram search ? returned to frontend for user selection."""
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


class JoinGroupRequest(BaseModel):
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


class JoinInfo(BaseModel):
    """Status of daily join limit."""
    joined_today: int
    max_daily: int
    remaining: int
