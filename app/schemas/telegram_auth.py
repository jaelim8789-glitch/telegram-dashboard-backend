from pydantic import BaseModel, Field

from app.schemas.account import AccountStatus


class SendCodeResponse(BaseModel):
    sent: bool


class VerifyCodeRequest(BaseModel):
    code: str = Field(min_length=1, max_length=10)


class Verify2FARequest(BaseModel):
    password: str = Field(min_length=1)


class AuthStepResult(BaseModel):
    status: AccountStatus
    requires_2fa: bool = False
    detail: str | None = None
