from typing import Literal

from pydantic import BaseModel, Field

GroupType = Literal["group", "megagroup", "channel"]


class GroupRead(BaseModel):
    id: str
    title: str
    type: GroupType
    participants_count: int | None = None


class GroupRecoveryInfo(BaseModel):
    """Information about a group's recovery/restoration state."""
    group_id: str
    title: str
    type: GroupType
    is_recoverable: bool = False
    recovery_action: str | None = None  # e.g. "rejoin", "re-auth required"
    last_known_membership: str | None = None


class GroupListParams(BaseModel):
    search: str | None = Field(default=None)
    type_filter: GroupType | None = Field(default=None)
    sort_by: str = Field(default="title")
    sort_dir: str = Field(default="asc")
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=50, ge=1, le=200)


class PaginatedGroups(BaseModel):
    items: list[GroupRead]
    total: int
    page: int
    page_size: int
    total_pages: int
