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
    # Admin-issued keys default to the "team" plan (effectively unlimited) rather
    # than inheriting whatever plan the tenant signed up under — an admin handing
    # out a key manually is presumed to want it unrestricted unless told otherwise.
    plan: str | None = Field(None, description="Plan to apply to the tenant (free/pro/team). Defaults to team.")


class ManualIssueResponse(BaseModel):
    user_id: str
    phone: str
    api_key: str
    already_issued: bool = False


class GuideHubPublishResponse(BaseModel):
    chat_id: str
    message_id: int
    created: bool


class AdminDashboardUserStats(BaseModel):
    total: int
    active: int
    inactive: int


class AdminDashboardAccountStats(BaseModel):
    total: int
    healthy: int
    unhealthy: int
    not_configured: int
    banned: int
    rate_limited: int
    unauthorized: int
    error_count: int
    unknown: int
    has_session: int
    has_errors: int
    total_today_sent: int
    total_groups: int


class AdminDashboardBroadcastStats(BaseModel):
    recent_total: int
    recent_failed: int
    failure_rate: float
    recent_window_hours: int = 24


class AdminDashboardStatusResponse(BaseModel):
    users: AdminDashboardUserStats
    accounts: AdminDashboardAccountStats
    broadcasts: AdminDashboardBroadcastStats
