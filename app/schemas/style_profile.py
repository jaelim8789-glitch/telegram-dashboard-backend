from datetime import datetime

from pydantic import BaseModel, Field, model_validator


class StyleProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    source_type: str = Field(default="text", pattern="^(text|channel)$")
    source_text: str = Field(default="", max_length=50000)
    account_id: str | None = Field(None, description="source_type=channel일 때 필수")
    chat_id: str | None = Field(None, description="source_type=channel일 때 필수 (채널 ID)")
    message_limit: int = Field(default=50, ge=1, le=200)


class StyleProfileAnalyzeRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    source_type: str = Field(default="text", pattern="^(text|channel)$")
    source_text: str = Field(default="", max_length=50000)
    account_id: str | None = Field(None, description="source_type=channel일 때 필수")
    chat_id: str | None = Field(None, description="source_type=channel일 때 필수 (채널 ID)")
    message_limit: int = Field(default=50, ge=1, le=200)

    @model_validator(mode="after")
    def _validate_source(self):
        if self.source_type == "text" and not self.source_text:
            raise ValueError("source_type=text인 경우 source_text는 필수입니다.")
        if self.source_type == "channel":
            if not self.account_id:
                raise ValueError("source_type=channel인 경우 account_id는 필수입니다.")
            if not self.chat_id:
                raise ValueError("source_type=channel인 경우 chat_id는 필수입니다.")
        return self


class StyleProfileUpdate(BaseModel):
    name: str | None = Field(None, max_length=200)


class StyleProfileRead(BaseModel):
    model_config = {"from_attributes": True}

    id: str
    name: str
    source_type: str
    source_text: str
    tone_analysis: dict
    style_prompt: str
    created_at: datetime
    updated_at: datetime
