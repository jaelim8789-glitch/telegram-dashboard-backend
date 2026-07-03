from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MatchType = Literal["keyword", "exact"]
AutoReplyLogStatus = Literal["success", "failed", "rate_limited"]


class AutoReplyRuleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    match_type: MatchType
    match_value: str = Field(min_length=1)
    reply_content: str = Field(min_length=1, max_length=4096)
    is_active: bool = True
    cooldown_hours: int = Field(default=1, ge=0)
    max_replies_per_day: int = Field(default=100, ge=1)


class AutoReplyRuleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    match_type: MatchType | None = None
    match_value: str | None = Field(default=None, min_length=1)
    reply_content: str | None = Field(default=None, min_length=1, max_length=4096)
    is_active: bool | None = None
    cooldown_hours: int | None = Field(default=None, ge=0)
    max_replies_per_day: int | None = Field(default=None, ge=1)


class AutoReplyRuleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    account_id: str
    name: str
    is_active: bool
    match_type: MatchType
    match_value: str
    reply_content: str
    cooldown_hours: int
    max_replies_per_day: int
    created_at: datetime
    updated_at: datetime


class AutoReplySettingsRead(BaseModel):
    """GET /api/accounts/{id}/auto-reply — the master switch plus every rule under it."""

    account_id: str
    auto_reply_enabled: bool
    rules: list[AutoReplyRuleRead]


class AutoReplyToggleRequest(BaseModel):
    enabled: bool


class AutoReplyToggleResponse(BaseModel):
    account_id: str
    auto_reply_enabled: bool


class AutoReplyLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    rule_id: str
    account_id: str
    chat_id: str
    user_id: str
    user_name: str | None
    trigger_message: str
    reply_sent: str
    status: AutoReplyLogStatus
    created_at: datetime
