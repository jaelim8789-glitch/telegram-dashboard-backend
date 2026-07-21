from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class CalendarEntry(BaseModel):
    id: str
    title: str
    scheduled_at: datetime | None = None
    status: str
    broadcast_id: str | None = None
    campaign_id: str | None = None

    model_config = {"from_attributes": True}


class SyncResponse(BaseModel):
    synced: int
