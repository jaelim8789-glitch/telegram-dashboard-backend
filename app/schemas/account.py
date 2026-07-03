from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

AccountStatus = Literal["active", "inactive", "banned"]


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
    created_at: datetime
    updated_at: datetime
    # session_data intentionally excluded — never serialized back to clients.
