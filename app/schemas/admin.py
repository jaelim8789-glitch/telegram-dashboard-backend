from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class AdminLoginRequest(BaseModel):
    username: str
    password: str


class AdminTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class AdminMeResponse(BaseModel):
    username: str


class UserLookupResponse(BaseModel):
    model_config = {"from_attributes": True}

    user_id: str | None = None
    phone: str | None = None
    is_active: bool | None = None
    created_at: datetime | None = None
    last_login: datetime | None = None
    has_api_key: bool = False

    tenant_id: str | None = None
    tenant_plan: str | None = None
    trial_expires_at: datetime | None = None
    subscription_status: str | None = None

    telegram_verification_status: str | None = None
    telegram_user_id: int | None = None
    telegram_verified_at: datetime | None = None


class ManualIssueRequest(BaseModel):
    user_identifier: str = Field(min_length=1, max_length=50, description="Phone number or tg_<telegram_user_id>")
    memo: str | None = Field(None, max_length=500)


class ManualIssueResponse(BaseModel):
    user_id: str
    phone: str
    api_key: str
    already_issued: bool = False
