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


class VerifyCodeResponse(BaseModel):
    """The only response that ever carries the full API key — shown once, at issuance."""

    api_key: str


class LoginWithApiKeyRequest(BaseModel):
    api_key: str = Field(min_length=1)


class LoginWithApiKeyResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class MeResponse(BaseModel):
    role: Literal["admin", "user", "api_key"]
    phone: str | None = None
    subscription_status: str | None = None
    plan: str | None = None
    trial_expires_at: datetime | None = None
