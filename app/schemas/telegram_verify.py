from typing import Literal

from pydantic import BaseModel, Field


class TelegramVerifyStartResponse(BaseModel):
    token: str
    bot_deep_link: str
    channel_url: str


class TelegramVerifyCheckRequest(BaseModel):
    token: str = Field(min_length=1, max_length=36)


class TelegramVerifyCheckResponse(BaseModel):
    status: Literal["pending_bot_start", "verified", "unverified"]
    reason: str | None = None


class FreeApiKeyIssueRequest(BaseModel):
    token: str = Field(min_length=1, max_length=36)
    phone: str = Field(min_length=1, max_length=50)
