"""Pydantic schemas for Message Template CRUD."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CategoryType = Literal["general", "promotion", "notice", "welcome", "follow_up", "alert"]


class MessageTemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    category: CategoryType = "general"
    content: str = Field(min_length=1, max_length=10000)
    variables: list[str] = []


class MessageTemplateUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    category: CategoryType | None = None
    content: str | None = Field(default=None, min_length=1, max_length=10000)
    variables: list[str] | None = None
    is_favorite: bool | None = None


class MessageTemplateRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    name: str
    category: str
    content: str
    variables: str  # JSON array
    is_favorite: bool
    use_count: int
    created_at: datetime
    updated_at: datetime


class MessageTemplateList(BaseModel):
    items: list[MessageTemplateRead]
    total: int