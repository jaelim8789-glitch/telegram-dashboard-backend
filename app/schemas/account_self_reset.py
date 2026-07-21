from pydantic import BaseModel, Field


class SelfResetSendCodeRequest(BaseModel):
    phone: str = Field(min_length=1, max_length=50)


class SelfResetVerifyCodeRequest(BaseModel):
    phone: str = Field(min_length=1, max_length=50)
    code: str = Field(min_length=1, max_length=10)


class SelfResetVerify2FARequest(BaseModel):
    phone: str = Field(min_length=1, max_length=50)
    password: str = Field(min_length=1)


class SelfResetResult(BaseModel):
    reset: bool
    requires_2fa: bool = False
    detail: str | None = None
