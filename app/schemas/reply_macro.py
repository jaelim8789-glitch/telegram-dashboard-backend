import json
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ReplyMacroCreate(BaseModel):
    target_chats: list[str] = Field(min_length=1, description="채팅방/그룹 ID 목록")
    message_content: str = Field(min_length=1, max_length=4096, description="홍보 메시지 내용")


class ReplyMacroRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    account_id: str
    name: str
    target_chats: list[str]
    message_content: str
    media_path: str | None
    used_targets: list[dict] = []
    created_at: datetime
    updated_at: datetime

    @field_validator("target_chats", mode="before")
    @classmethod
    def _deserialize_target_chats(cls, value: object) -> list[str]:
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, list):
                    return parsed
            except (json.JSONDecodeError, TypeError):
                pass
            return [c.strip() for c in value.split(",") if c.strip()]
        if isinstance(value, list):
            return value
        return []

    @field_validator("used_targets", mode="before")
    @classmethod
    def _deserialize_used_targets(cls, value: object) -> list[dict]:
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, list):
                    return parsed
            except (json.JSONDecodeError, TypeError):
                pass
            return []
        if isinstance(value, list):
            return value
        return []