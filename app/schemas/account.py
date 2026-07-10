from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

AccountStatus = Literal["active", "inactive", "banned"]

HealthStatus = Literal[
    "healthy", "unauthorized", "banned", "rate_limited",
    "error", "unknown", "not_configured",
]


class AccountCreate(BaseModel):
    phone: str = Field(min_length=1, max_length=50)
    name: str | None = Field(default=None, max_length=100)


class AccountUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    status: AccountStatus | None = None
    today_sent: int | None = Field(default=None, ge=0)
    group_count: int | None = Field(default=None, ge=0)


class AccountRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    phone: str
    name: str | None
    status: AccountStatus
    today_sent: int
    group_count: int
    last_activity: datetime | None
    auto_reply_enabled: bool
    last_error: str | None
    last_error_at: datetime | None
    last_success_at: datetime | None
    health_checked_at: datetime | None
    created_at: datetime
    updated_at: datetime


class AccountWithHealth(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    phone: str
    name: str | None
    status: AccountStatus
    health_status: HealthStatus
    has_session: bool
    today_sent: int
    group_count: int
    last_activity: datetime | None
    last_error: str | None
    last_error_at: datetime | None
    last_success_at: datetime | None
    health_checked_at: datetime | None
    auto_reply_enabled: bool
    recent_success_count: int
    recent_failure_count: int
    total_delivery_attempts: int
    created_at: datetime
    updated_at: datetime


# ── Search / Filter / Sort / Pagination ─────────────────────────────────


class AccountFilterParams(BaseModel):
    search: str | None = Field(default=None, description="Search phone or name")
    status: AccountStatus | None = None
    health_status: HealthStatus | None = None
    has_session: bool | None = None
    has_error: bool | None = None
    auto_reply_enabled: bool | None = None
    phone: str | None = None


class AccountSortParams(BaseModel):
    sort_by: str = Field(default="created_at", description="Field to sort by")
    sort_dir: str = Field(default="desc", description="asc or desc")


class AccountListParams(BaseModel):
    filter: AccountFilterParams = Field(default_factory=AccountFilterParams)
    sort: AccountSortParams = Field(default_factory=AccountSortParams)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)


class PaginatedAccounts(BaseModel):
    items: list[AccountWithHealth]
    total: int
    page: int
    page_size: int
    total_pages: int


# ── Bulk Operations ─────────────────────────────────────────────────────


class BulkActionRequest(BaseModel):
    account_ids: list[str] = Field(min_length=1, max_length=100)
    action: str = Field(description="activate, deactivate, delete, reset_session")


class BulkActionResult(BaseModel):
    account_id: str
    success: bool
    error: str | None = None


class BulkActionResponse(BaseModel):
    results: list[BulkActionResult]
    total_processed: int
    total_failed: int


# ── Operational Summary ──────────────────────────────────────────────────


class AccountSummary(BaseModel):
    total: int
    healthy: int
    unhealthy: int
    not_configured: int
    banned: int
    rate_limited: int
    unauthorized: int
    active_accounts: int
    inactive_accounts: int
    has_session: int
    has_errors: int
    total_today_sent: int
    total_groups: int
