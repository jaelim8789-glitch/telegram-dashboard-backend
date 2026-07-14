from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

BroadcastStatus = Literal["pending", "sending", "sent", "failed", "cancelled"]

DeliveryMode = Literal["normal", "cycle", "bulk", "reply"]


RECURRING_INTERVAL_VALUES = {30, 60, 120, 180, 360, 720, 1440}


class BroadcastCreate(BaseModel):
    account_id: str
    message: str = Field(default="", max_length=4096)
    recipients: list[str] = Field(min_length=1)
    scheduled_at: datetime | None = None
    recurring_interval_minutes: int | None = None
    delivery_mode: DeliveryMode = "normal"
    reply_to_msg_id: int | None = None
    delay_seconds: int | None = Field(default=None, ge=1, le=3600)

    @model_validator(mode="after")
    def _validate_message_or_reply(self):
        if not self.message and self.reply_to_msg_id is None:
            raise ValueError("message or reply_to_msg_id is required")
        return self

    @classmethod
    def validate_recurring_interval(cls, v: int | None) -> int | None:
        if v is not None and v not in RECURRING_INTERVAL_VALUES:
            raise ValueError(
                f"recurring_interval_minutes must be one of {sorted(RECURRING_INTERVAL_VALUES)}, got {v}"
            )
        return v


class BroadcastRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    account_id: str
    message: str
    media_path: str | None
    recipients: list[str]
    status: BroadcastStatus
    scheduled_at: datetime | None
    sent_at: datetime | None
    created_at: datetime
    error_message: str | None
    recurring_interval_minutes: int | None = None
    cancelled_at: datetime | None = None
    next_scheduled_at: datetime | None = None
    parent_broadcast_id: str | None = None
    is_recurring_paused: bool = False
    failure_info: dict | None = None
    delivery_mode: DeliveryMode = "normal"
    reply_to_msg_id: int | None = None
    delay_seconds: int | None = None


class BroadcastChildrenRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    account_id: str
    message: str
    status: BroadcastStatus
    scheduled_at: datetime | None
    sent_at: datetime | None
    created_at: datetime
    error_message: str | None
    failure_info: dict | None = None
    reply_to_msg_id: int | None = None


class BroadcastWithChildCount(BroadcastRead):
    child_count: int = 0
    last_child_status: str | None = None
    last_child_created_at: datetime | None = None
