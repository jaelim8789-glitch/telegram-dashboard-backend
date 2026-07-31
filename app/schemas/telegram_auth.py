from pydantic import BaseModel, Field

from app.schemas.account import AccountStatus


class SendCodeResponse(BaseModel):
    sent: bool
    # "telegram_app" | "sms" | "call" | "flash_call" | None (unknown)
    channel: str | None = None
    # Server-generated hint so clients don't have to re-map channel codes.
    delivery_hint: str | None = None


class VerifyCodeRequest(BaseModel):
    code: str = Field(min_length=1, max_length=10)


class Verify2FARequest(BaseModel):
    password: str = Field(min_length=1)


class AuthStepResult(BaseModel):
    status: AccountStatus
    requires_2fa: bool = False
    detail: str | None = None
