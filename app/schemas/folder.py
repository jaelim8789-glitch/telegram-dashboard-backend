import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SmartFolderType = Literal["recent_activity", "unsent", "vip", "auto_classify"]


def _deserialize_json_list(value: object) -> list[str]:
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


class FolderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = ""
    color: str = Field(default="#6366f1", max_length=20)
    icon: str = Field(default="folder", max_length=50)
    group_ids: list[str] = []
    parent_id: str | None = None


class FolderUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = None
    color: str | None = Field(default=None, max_length=20)
    icon: str | None = Field(default=None, max_length=50)
    group_ids: list[str] | None = None
    order: int | None = None
    parent_id: str | None = None
    is_collapsed: bool | None = None


class SmartFolderConfig(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    smart_type: SmartFolderType
    color: str = Field(default="#22c55e", max_length=20)
    icon: str = Field(default="sparkles", max_length=50)
    description: str = ""
    params: dict[str, Any] = {}


class FolderReorderInput(BaseModel):
    folder_id: str
    order: int
    parent_id: str | None = None


class BatchMoveInput(BaseModel):
    source_folder_id: str | None = None
    target_folder_id: str | None = None
    group_ids: list[str] = Field(min_length=1)


class FolderSendInput(BaseModel):
    folder_ids: list[str] = Field(min_length=1)
    message: str = Field(min_length=1, max_length=4096)
    exclude_group_ids: list[str] = []


class WorkspaceStateInput(BaseModel):
    collapsed_folder_ids: list[str] = []
    pinned_folder_ids: list[str] = []


class FolderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    account_id: str
    name: str
    description: str
    color: str
    icon: str
    group_ids: list[str]
    order: int
    parent_id: str | None
    is_collapsed: bool
    is_smart: bool
    smart_type: str | None
    created_at: datetime
    updated_at: datetime
    children: list["FolderRead"] | None = None

    @field_validator("group_ids", mode="before")
    @classmethod
    def _deserialize_group_ids(cls, value: object) -> list[str]:
        return _deserialize_json_list(value)
