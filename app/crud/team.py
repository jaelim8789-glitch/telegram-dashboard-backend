"""CRUD operations for TeamMember management."""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.team import TeamMember
from app.schemas.team import TeamMemberInvite, TeamMemberUpdate


async def list_members(
    db: AsyncSession,
    tenant_id: str,
    skip: int = 0,
    limit: int = 50,
    search: str | None = None,
) -> tuple[list[TeamMember], int]:
    """List team members for a tenant, with optional search."""
    conditions = [TeamMember.tenant_id == tenant_id]
    if search:
        conditions.append(
            or_(
                TeamMember.username.ilike(f"%{search}%"),
                TeamMember.display_name.ilike(f"%{search}%"),
            )
        )

    count_q = select(func.count()).select_from(TeamMember).where(*conditions)
    total = await db.scalar(count_q) or 0

    q = (
        select(TeamMember)
        .where(*conditions)
        .order_by(TeamMember.created_at.desc())
        .offset(skip)
        .limit(limit)
    )
    result = await db.execute(q)
    items = list(result.scalars().all())
    return items, total


async def get_member(db: AsyncSession, member_id: str) -> TeamMember | None:
    """Get a single team member by ID."""
    result = await db.execute(select(TeamMember).where(TeamMember.id == member_id))
    return result.scalar_one_or_none()


async def get_member_by_username(db: AsyncSession, tenant_id: str, username: str) -> TeamMember | None:
    """Get a team member by tenant_id and username."""
    result = await db.execute(
        select(TeamMember).where(
            TeamMember.tenant_id == tenant_id,
            TeamMember.username == username,
        )
    )
    return result.scalar_one_or_none()


async def get_member_by_invite_token(db: AsyncSession, token: str) -> TeamMember | None:
    """Get a team member by invite token."""
    result = await db.execute(
        select(TeamMember).where(TeamMember.invite_token == token)
    )
    return result.scalar_one_or_none()


async def create_owner(db: AsyncSession, tenant_id: str, username: str) -> TeamMember:
    """Create the initial owner member for a new tenant."""
    member = TeamMember(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        username=username,
        role="owner",
        is_active=True,
        joined_at=datetime.now(timezone.utc),
    )
    db.add(member)
    await db.commit()
    await db.refresh(member)
    return member


async def invite_member(
    db: AsyncSession,
    tenant_id: str,
    inviter_id: str,
    payload: TeamMemberInvite,
) -> TeamMember:
    """Invite a new member. Creates a pending member with invite token."""
    import secrets
    token = secrets.token_hex(32)

    member = TeamMember(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        username=payload.username,
        role=payload.role,
        is_active=False,
        invited_by=inviter_id,
        invite_token=token,
        invite_expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        invited_at=datetime.now(timezone.utc),
    )
    db.add(member)
    await db.commit()
    await db.refresh(member)
    return member


async def accept_invite(db: AsyncSession, token: str) -> TeamMember | None:
    """Accept an invite by token. Returns the member if successful."""
    member = await get_member_by_invite_token(db, token)
    if not member:
        return None
    if member.invite_expires_at and member.invite_expires_at < datetime.now(timezone.utc):
        return None
    if member.joined_at:
        return None  # Already accepted

    member.is_active = True
    member.joined_at = datetime.now(timezone.utc)
    member.invite_token = None  # Consume the token
    member.invite_expires_at = None
    await db.commit()
    await db.refresh(member)
    return member


async def update_member(
    db: AsyncSession,
    member: TeamMember,
    payload: TeamMemberUpdate,
) -> TeamMember:
    """Update a team member's role, status, or display name."""
    update_data = payload.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(member, field, value)
    await db.commit()
    await db.refresh(member)
    return member


async def remove_member(db: AsyncSession, member: TeamMember) -> None:
    """Remove (delete) a team member."""
    await db.delete(member)
    await db.commit()


async def count_owners(db: AsyncSession, tenant_id: str) -> int:
    """Count how many owners a tenant has (to prevent removing the last owner)."""
    q = select(func.count()).select_from(TeamMember).where(
        TeamMember.tenant_id == tenant_id,
        TeamMember.role == "owner",
        TeamMember.is_active == True,
    )
    result = await db.scalar(q)
    return result or 0


async def touch_last_login(db: AsyncSession, member: TeamMember) -> None:
    """Update the last_login timestamp."""
    member.last_login = datetime.now(timezone.utc)
    await db.commit()