"""Pydantic schemas for Team management."""

from datetime import datetime

from pydantic import BaseModel, Field


class TeamMemberRead(BaseModel):
    """Response schema for a team member."""
    id: str
    tenant_id: str
    username: str
    display_name: str | None = None
    phone: str | None = None
    role: str = "member"  # owner, admin, member
    is_active: bool = True
    invited_by: str | None = None
    invited_at: datetime | None = None
    joined_at: datetime | None = None
    last_login: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class TeamMemberList(BaseModel):
    """List of team members with total count."""
    items: list[TeamMemberRead]
    total: int


class TeamMemberInvite(BaseModel):
    """Invite a new team member."""
    username: str = Field(..., min_length=1, max_length=100, description="초대할 멤버의 사용자명")
    role: str = Field(default="member", pattern="^(admin|member)$", description="admin 또는 member")


class TeamMemberUpdate(BaseModel):
    """Update a team member's role or status."""
    role: str | None = Field(default=None, pattern="^(admin|member)$")
    is_active: bool | None = None
    display_name: str | None = Field(default=None, max_length=100)


class TeamMemberInviteAccept(BaseModel):
    """Accept an invite with token."""
    invite_token: str = Field(..., min_length=1, max_length=64)