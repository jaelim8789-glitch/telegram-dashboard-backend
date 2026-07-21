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
    telegram_verification_token: str | None = None
    referral_code: str | None = Field(default=None, max_length=30)


class VerifyCodeResponse(BaseModel):
    """The only response that ever carries the full API key — shown once, at issuance."""

    api_key: str


class LoginWithApiKeyRequest(BaseModel):
    api_key: str = Field(min_length=1)


class LinkTelegramRequest(BaseModel):
    telegram_id: int = Field(..., description="Telegram 사용자 ID")
    telegram_username: str | None = Field(default=None, max_length=255)
    telegram_photo_url: str | None = Field(default=None, max_length=512)


class LinkTelegramResponse(BaseModel):
    linked: bool
    telegram_id: int
    plan: str | None
    subscription_status: str | None
    message: str = ""


class LoginWithApiKeyResponse(BaseModel):
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
    referral_code: str | None = Field(default=None, max_length=30)


class TelegramLoginResponse(BaseModel):
    access_token: str
    session_token: str
    token_type: str = "bearer"
    is_new_user: bool = False
