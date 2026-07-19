from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class SendCodeRequest(BaseModel):
    phone: str = Field(min_length=1, max_length=50)


class SendCodeResponse(BaseModel):
    sent: bool


class VerifyCodeRequest(BaseModel):
    phone: str = Field(min_length=1, max_length=50)
    code: str = Field(min_length=1, max_length=10)
    # Required only for a brand-new signup (no Tenant yet for this phone) — proves
    # official-channel membership was verified server-side before a trial is created.
    # Returning users (existing Tenant) don't need it. See app/api/telegram_verify.py.
    telegram_verification_token: str | None = None


class VerifyCodeResponse(BaseModel):
    """The only response that ever carries the full API key — shown once, at issuance."""

    api_key: str


class LoginWithApiKeyRequest(BaseModel):
    api_key: str = Field(min_length=1)


class LoginWithApiKeyResponse(BaseModel):
    access_token: str
    session_token: str | None = None
    token_type: str = "bearer"


class TelegramLoginRequest(BaseModel):
    phone: str = Field(min_length=1, max_length=50)
    code: str = Field(min_length=1, max_length=10)


class TelegramLoginResponse(BaseModel):
    access_token: str
    session_token: str | None = None
    token_type: str = "bearer"


class MeResponse(BaseModel):
    role: Literal["admin", "user", "api_key"]
    phone: str | None = None
    subscription_status: str | None = None
    plan: str | None = None
    trial_expires_at: datetime | None = None
    telegram_username: str | None = None
    telegram_photo_url: str | None = None
    stars_balance: int = 0


class TelegramLoginRequest(BaseModel):
    id: int
    first_name: str
    last_name: str | None = None
    username: str | None = None
    photo_url: str | None = None
    auth_date: int
    hash: str


class TelegramLoginResponse(BaseModel):
    access_token: str
    session_token: str
    token_type: str = "bearer"
    is_new_user: bool = False
