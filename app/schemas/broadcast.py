from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.limits import MAX_RECIPIENTS_PER_BROADCAST

BroadcastStatus = Literal["pending", "sending", "sent", "failed"]


class BroadcastCreate(BaseModel):
    account_id: str
    message: str = Field(min_length=1, max_length=4096)
    recipients: list[str] = Field(min_length=1, max_length=MAX_RECIPIENTS_PER_BROADCAST)
    scheduled_at: datetime | None = None


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
