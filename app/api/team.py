"""API router for Team management — tenant-scoped member CRUD, invites, roles."""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_identity, Identity, require_tenant_access
from app.core.logging import get_logger
from app.crud import team as team_crud
from app.database import get_db
from app.schemas.team import (
    TeamMemberInvite,
    TeamMemberInviteAccept,
    TeamMemberList,
    TeamMemberRead,
    TeamMemberUpdate,
)

router = APIRouter(prefix="/api/tenants/{tenant_id}/team", tags=["team"])
logger = get_logger(__name__)


@router.get("/members", response_model=TeamMemberList)
async def list_team_members(
    tenant_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
    search: str | None = Query(None, max_length=200),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
):
    """List all members of a team (tenant)."""
    await require_tenant_access(tenant_id, identity)
    items, total = await team_crud.list_members(
        db, tenant_id, skip=skip, limit=limit, search=search,
    )
    return TeamMemberList(
        items=[TeamMemberRead.model_validate(m) for m in items],
        total=total,
    )


@router.get("/members/{member_id}", response_model=TeamMemberRead)
async def get_team_member(
    tenant_id: str,
    member_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Get a single team member by ID."""
    await require_tenant_access(tenant_id, identity)
    member = await team_crud.get_member(db, member_id)
    if member is None or member.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="멤버를 찾을 수 없습니다.")
    return member


@router.post("/invite", response_model=TeamMemberRead, status_code=status.HTTP_201_CREATED)
async def invite_team_member(
    tenant_id: str,
    payload: TeamMemberInvite,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Invite a new member to the team. Requires admin or owner role."""
    await require_tenant_access(tenant_id, identity)

    # Resolve inviter's member record (the current identity's tenant member)
    inviter = await _resolve_current_member(db, tenant_id, identity)
    if inviter is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="팀 멤버 정보를 찾을 수 없습니다.")
    if inviter.role not in ("owner", "admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="멤버 초대 권한이 없습니다. Owner 또는 Admin만 초대할 수 있습니다.")

    # Check for duplicate username
    existing = await team_crud.get_member_by_username(db, tenant_id, payload.username)
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="이미 존재하는 사용자명입니다.")

    member = await team_crud.invite_member(db, tenant_id, inviter.id, payload)
    logger.info("team_invite_sent", tenant_id=tenant_id, username=payload.username, role=payload.role)
    return member


@router.post("/accept-invite", response_model=TeamMemberRead)
async def accept_team_invite(
    payload: TeamMemberInviteAccept,
    db: AsyncSession = Depends(get_db),
):
    """Accept a team invite using the invite token. No auth required (token-based)."""
    member = await team_crud.accept_invite(db, payload.invite_token)
    if member is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="초대를 찾을 수 없거나 만료되었습니다.")
    logger.info("team_invite_accepted", tenant_id=member.tenant_id, username=member.username)
    return member


@router.put("/members/{member_id}", response_model=TeamMemberRead)
async def update_team_member(
    tenant_id: str,
    member_id: str,
    payload: TeamMemberUpdate,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Update a team member's role, status, or display name. Requires admin/owner."""
    await require_tenant_access(tenant_id, identity)
    inviter = await _resolve_current_member(db, tenant_id, identity)
    if inviter is None or inviter.role not in ("owner", "admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="멤버 관리 권한이 없습니다.")

    member = await team_crud.get_member(db, member_id)
    if member is None or member.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="멤버를 찾을 수 없습니다.")

    # Only owner can change another owner's role
    if member.role == "owner" and inviter.role != "owner":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Owner 권한을 변경할 수 없습니다.")

    # Prevent removing the last owner
    if payload.role is not None and payload.role != "owner" and member.role == "owner":
        owner_count = await team_crud.count_owners(db, tenant_id)
        if owner_count <= 1:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="마지막 Owner는 역할을 변경할 수 없습니다.")

    updated = await team_crud.update_member(db, member, payload)
    logger.info("team_member_updated", tenant_id=tenant_id, member_id=member_id, role=updated.role)
    return updated


@router.delete("/members/{member_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_team_member(
    tenant_id: str,
    member_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Remove a member from the team. Requires admin/owner."""
    await require_tenant_access(tenant_id, identity)
    inviter = await _resolve_current_member(db, tenant_id, identity)
    if inviter is None or inviter.role not in ("owner", "admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="멤버 관리 권한이 없습니다.")

    member = await team_crud.get_member(db, member_id)
    if member is None or member.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="멤버를 찾을 수 없습니다.")

    # Only owner can remove another owner
    if member.role == "owner" and inviter.role != "owner":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Owner를 제거할 수 없습니다.")

    # Prevent removing the last owner
    if member.role == "owner":
        owner_count = await team_crud.count_owners(db, tenant_id)
        if owner_count <= 1:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="마지막 Owner는 제거할 수 없습니다.")

    await team_crud.remove_member(db, member)
    logger.info("team_member_removed", tenant_id=tenant_id, member_id=member_id)


async def _resolve_current_member(
    db: AsyncSession,
    tenant_id: str,
    identity: Identity,
) -> "TeamMember | None":
    """Resolve the current identity's TeamMember record for this tenant."""
    from sqlalchemy import select
    from app.models.team import TeamMember as TeamMemberModel

    # For admin, find the first owner member as context
    if identity.kind == "admin":
        result = await db.execute(
            select(TeamMemberModel).where(
                TeamMemberModel.tenant_id == tenant_id,
                TeamMemberModel.role == "owner",
            ).limit(1)
        )
        return result.scalar_one_or_none()

    # For user/api_key, must match tenant
    if identity.tenant_id != tenant_id:
        return None

    # Find the first active member for this tenant
    result = await db.execute(
        select(TeamMemberModel).where(
            TeamMemberModel.tenant_id == tenant_id,
            TeamMemberModel.is_active == True,
        ).limit(1)
    )
    return result.scalar_one_or_none()
