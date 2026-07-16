from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


CampaignGoal = Literal["awareness", "engagement", "conversion", "retention"]
CampaignStatus = Literal["draft", "active", "paused", "completed", "cancelled"]


class CampaignCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: str | None = None
    goal: str | None = Field(default=None, max_length=50)


class CampaignUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    goal: str | None = Field(default=None, max_length=50)
    status: str | None = Field(default=None, max_length=20)


class CampaignRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    name: str
    description: str | None = None
    status: str = "draft"
    goal: str | None = None
    total_broadcasts: int = 0
    total_sent: int = 0
    total_failed: int = 0
    total_recipients: int = 0
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None


class CampaignList(BaseModel):
    items: list[CampaignRead]
    total: int