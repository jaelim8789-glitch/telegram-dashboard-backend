from datetime import datetime

from pydantic import BaseModel, ConfigDict


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    phone: str
    is_active: bool
    created_at: datetime
    last_login: datetime | None


class UserToggleRequest(BaseModel):
    is_active: bool


class UserApiKeyReissued(BaseModel):
    """The only response that ever carries the full API key — shown once, at reissue."""

    id: str
    api_key: str
