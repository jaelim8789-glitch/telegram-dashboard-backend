from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ReplyMacroCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    target_chats: list[str] = Field(min_length=1, description="List of chat/group IDs to send to")
    message_content: str = Field(min_length=1, max_length=4096)
    schedule_type: Literal["interval", "fixed"] = "interval"
    interval_hours: int = Field(default=24, ge=1)
    fixed_time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$", description="HH:MM format")
    max_sends_per_day: int = Field(default=10, ge=1)
    is_active: bool = True


class ReplyMacroUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    target_chats: list[str] | None = None
    message_content: str | None = Field(default=None, min_length=1, max_length=4096)
    schedule_type: Literal["interval", "fixed"] | None = None
    interval_hours: int | None = Field(default=None, ge=1)
    fixed_time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    max_sends_per_day: int | None = Field(default=None, ge=1)
    is_active: bool | None = None


class ReplyMacroRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    account_id: str
    name: str
    is_active: bool
    target_chats: str  # JSON string
    message_content: str
    media_path: str | None
    schedule_type: str
    interval_hours: int
    fixed_time: str | None
    max_sends_per_day: int
    last_sent_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ReplyMacroLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    macro_id: str
    account_id: str
    target_chat_id: str
    message_sent: str
    status: str
    error_message: str | None
    created_at: datetime