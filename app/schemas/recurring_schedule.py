from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RecurringScheduleCreate(BaseModel):
    account_id: str
    message: str = Field(min_length=1, max_length=4096)
    interval_minutes: int = Field(ge=30, le=1440)
    # recipients stored as JSON in the broadcast table, but here we persist them
    # as part of the schedule itself.
    group_ids: list[str] = Field(min_length=1)


class RecurringScheduleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    account_id: str
    message: str
    media_path: str | None
    interval_minutes: int
    is_active: bool
    total_sends: int
    last_sent_at: datetime | None
    created_at: datetime


class RecurringScheduleUpdate(BaseModel):
    is_active: bool
