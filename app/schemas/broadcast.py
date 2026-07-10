from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.limits import MAX_RECIPIENTS_PER_BROADCAST

BroadcastStatus = Literal["pending", "sending", "sent", "failed", "cancelled"]


RECURRING_INTERVAL_VALUES = {30, 60, 120, 180, 360, 720, 1440}


class BroadcastCreate(BaseModel):
    account_id: str
    message: str = Field(min_length=1, max_length=4096)
    recipients: list[str] = Field(min_length=1, max_length=MAX_RECIPIENTS_PER_BROADCAST)
    scheduled_at: datetime | None = None
    recurring_interval_minutes: int | None = None

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