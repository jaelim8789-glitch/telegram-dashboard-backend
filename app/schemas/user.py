from datetime import datetime

from pydantic import BaseModel, ConfigDict


class UserRead(BaseModel):
    """User + Tenant info for admin users list."""
    model_config = ConfigDict(from_attributes=True)

    id: str
    phone: str
    is_active: bool
    created_at: datetime
    last_login: datetime | None
    # Tenant fields (enriched)
    plan: str | None = None  # free, pro, team
    subscription_status: str | None = None  # active, inactive, pending, past_due, canceled
    trial_expires_at: datetime | None = None
    account_count: int = 0  # number of Telegram accounts linked to this user's tenant
    stars_balance: int = 0  # Telegram Stars balance


class UserToggleRequest(BaseModel):
    is_active: bool


class UserApiKeyReissued(BaseModel):
    """The only response that ever carries the full API key — shown once, at reissue."""

    id: str
    api_key: str
