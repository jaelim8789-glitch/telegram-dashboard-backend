from datetime import datetime

from pydantic import BaseModel, Field


class APIKeyCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class APIKeyCreated(BaseModel):
    """The only response that ever carries the full key — shown once, at creation."""

    id: str
    key: str
    name: str
    created_at: datetime


class APIKeyRead(BaseModel):
    id: str
    masked_key: str
    name: str
    is_active: bool
    created_at: datetime
    last_used: datetime | None
