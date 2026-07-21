from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
import time

from app.api.deps import get_current_identity, Identity, require_account_tenant_access
from app.crud import account as account_crud
from app.database import get_db
from app.schemas.group import GroupRead, GroupListParams, PaginatedGroups, GroupRecoveryInfo
from app.services.telegram_actions import AccountNotAuthenticatedError, get_folders, list_groups

router = APIRouter(prefix="/api/accounts", tags=["groups"])

_groups_cache: dict = {}
_groups_cache_ttl = 60


def _cached_groups_key(account_id: str) -> str:
    return f"groups:{account_id}"


async def _get_cached_groups(account_id: str):
    key = _cached_groups_key(account_id)
    now = time.time()
    if key in _groups_cache:
        data, ts = _groups_cache[key]
        if now - ts < _groups_cache_ttl:
            return data
    return None


def _set_cached_groups(account_id: str, data):
    _groups_cache[_cached_groups_key(account_id)] = (data, time.time())


@router.get("/{account_id}/groups", response_model=PaginatedGroups)
async def read_groups(
    account_id: str,
    search: str | None = Query(default=None, description="Search group title"),
    type_filter: str | None = Query(default=None, alias="type"),
    sort_by: str = Query(default="title", description="title, type, participants_count"),
    sort_dir: str = Query(default="asc"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """List groups/channels the account is in with search, filter, sort, pagination."""
    await require_account_tenant_access(account_id, db, identity)
    account = await account_crud.get_account(db, account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="계정을 찾을 수 없습니다.")

    try:
        all_groups = await _get_cached_groups(account_id)
        if all_groups is None:
            all_groups = await list_groups(account)
            _set_cached_groups(account_id, all_groups)
    except AccountNotAuthenticatedError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))

    # Search
    if search:
        pattern = search.lower()
        all_groups = [g for g in all_groups if pattern in g["title"].lower()]

    # Filter by type
    if type_filter:
        all_groups = [g for g in all_groups if g["type"] == type_filter]

    # Sort
    reverse = sort_dir == "desc"
    if sort_by == "title":
        all_groups.sort(key=lambda g: g.get("title", "").lower(), reverse=reverse)
    elif sort_by == "type":
        all_groups.sort(key=lambda g: g.get("type", ""), reverse=reverse)
    elif sort_by == "participants_count":
        all_groups.sort(key=lambda g: g.get("participants_count") or 0, reverse=reverse)

    # Paginate
    total = len(all_groups)
    offset = (page - 1) * page_size
    page_groups = all_groups[offset:offset + page_size]
    total_pages = max(1, (total + page_size - 1) // page_size)

    items = [GroupRead(**g) for g in page_groups]
    return PaginatedGroups(items=items, total=total, page=page, page_size=page_size, total_pages=total_pages)


@router.get("/{account_id}/groups/folders")
async def read_group_folders(
    account_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """List the account's Telegram chat folders (Dialog Filters), each with the
    group IDs it contains. Best-effort — returns an empty list rather than an
    error if Telegram folders can't be read, so the caller can always fall
    back to the plain group list."""
    await require_account_tenant_access(account_id, db, identity)
    account = await account_crud.get_account(db, account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="계정을 찾을 수 없습니다.")

    try:
        folders = await get_folders(account)
    except AccountNotAuthenticatedError:
        return []
    return folders


@router.get("/{account_id}/groups/discovery-info")
async def get_group_discovery_info(
    account_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Get group discovery stats for an account."""
    await require_account_tenant_access(account_id, db, identity)
    account = await account_crud.get_account(db, account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="계정을 찾을 수 없습니다.")

    try:
        all_groups = await list_groups(account)
    except AccountNotAuthenticatedError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="계정이 인증되지 않았습니다.")
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))

    groups = [g for g in all_groups if g["type"] in ("group", "megagroup")]
    channels = [g for g in all_groups if g["type"] == "channel"]

    return {
        "total_groups": len(all_groups),
        "groups": len(groups),
        "channels": len(channels),
        "top_groups": sorted(groups, key=lambda g: g.get("participants_count") or 0, reverse=True)[:10],
        "top_channels": sorted(channels, key=lambda g: g.get("participants_count") or 0, reverse=True)[:10],
    }
