from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

BroadcastStatus = Literal["pending", "sending", "sent", "failed", "cancelled"]

DeliveryMode = Literal["normal", "cycle", "bulk", "reply"]


RECURRING_INTERVAL_VALUES = {30, 60, 120, 180, 360, 720, 1440}


class BroadcastCreate(BaseModel):
    account_id: str
    message: str = Field(default="", max_length=4096)
    recipients: list[str] = Field(default_factory=list, min_length=0)
    scheduled_at: datetime | None = None
    recurring_interval_minutes: int | None = None
    delivery_mode: DeliveryMode = "normal"
    reply_to_msg_id: int | None = None
    delay_seconds: int | None = Field(default=None, ge=1, le=3600)
    inline_buttons: list[dict] | None = None
    # Send-to-group: if set, recipients will be resolved from group member lists at dispatch time
    group_ids: list[str] | None = None
    # Campaign linkage
    campaign_id: str | None = None

    @model_validator(mode="after")
    def _validate_message_or_reply(self):
        if not self.message and self.reply_to_msg_id is None:
            raise ValueError("message or reply_to_msg_id is required")
        return self

    @model_validator(mode="after")
    def _validate_recipients_or_group_ids(self):
        if not self.recipients and not self.group_ids:
            raise ValueError("recipients or group_ids is required")
        return self

    @classmethod
    def validate_recurring_interval(cls, v: int | None) -> int | None:
        if v is not None and v not in RECURRING_INTERVAL_VALUES:
            raise ValueError(
                f"recurring_interval_minutes must be one of {sorted(RECURRING_INTERVAL_VALUES)}, got {v}"
            )
        return v


class BroadcastSendGroupRequest(BaseModel):
    account_id: str
    message: str = Field(default="", max_length=4096)
    group_ids: list[str] = Field(min_length=1, description="List of group chat IDs to send to")
    scheduled_at: datetime | None = None
    delivery_mode: DeliveryMode = "normal"
    delay_seconds: int | None = Field(default=None, ge=1, le=3600)
    inline_buttons: list[dict] | None = None
    campaign_id: str | None = None


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
    inline_buttons: list[dict] | None = None
    group_ids: list[str] | None = None
    groups_resolved: bool = False
    campaign_id: str | None = None
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
    delivery_mode: DeliveryMode = "normal"
    reply_to_msg_id: int | None = None
    inline_buttons: list[dict] | None = None


class BroadcastWithChildCount(BroadcastRead):
    child_count: int = 0
    last_child_status: str | None = None
    last_child_created_at: datetime | None = None


class BatchRetryRequest(BaseModel):
    broadcast_ids: list[str] = Field(min_length=1, max_length=100)


class BatchRetryResult(BaseModel):
    results: list[dict]


class BroadcastEstimateRequest(BaseModel):
    account_id: str
    recipient_count: int = Field(ge=1, le=100000)
    delivery_mode: DeliveryMode = "normal"
    delay_seconds: int | None = None


class BroadcastEstimateResponse(BaseModel):
    estimated_seconds: int
    estimated_minutes: int
    readable: str