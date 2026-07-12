from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_identity, Identity, require_account_tenant_access, require_active_subscription
from app.core.logging import get_logger
from app.crud import account as account_crud
from app.crud import group_search as group_search_crud
from app.database import get_db
from app.schemas.group_search import (
    GroupJoinLogList,
    GroupJoinLogRead,
    GroupSearchRequest,
    GroupSearchResultList,
    GroupSearchResultRead,
    JoinGroupRequest,
    JoinInfo,
    GroupJoinStats,
)
from app.services.group_search_service import DailyJoinLimitExceededError, search_public_groups, join_selected_groups
from app.core.limits import MAX_DAILY_JOINS

router = APIRouter(prefix="/api/group-search", tags=["group-search"], dependencies=[Depends(require_active_subscription)])
logger = get_logger(__name__)


@router.post("/search", response_model=GroupSearchResultList)
async def search_groups(
    payload: GroupSearchRequest,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Search Telegram for public groups matching a keyword and store results."""
    await require_account_tenant_access(payload.account_id, db, identity)
    account = await account_crud.get_account(db, payload.account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="계정을 찾을 수 없습니다.")

    try:
        results = await search_public_groups(account, payload.keyword)
    except Exception as exc:
        logger.error("group_search_failed", account_id=account.id, keyword=payload.keyword, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"그룹 검색에 실패했습니다: {exc}",
        )

    if not results:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f'"{payload.keyword}" 검색 결과가 없습니다.',
        )

    saved = await group_search_crud.get_recent_results(db, account.id, keyword=payload.keyword)
    return GroupSearchResultList(items=saved[:100], total=len(saved), keyword=payload.keyword)


@router.get("/results/{account_id}", response_model=GroupSearchResultList)
async def get_search_results(
    account_id: str,
    keyword: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Get recent search results for an account with optional keyword filter."""
    await require_account_tenant_access(account_id, db, identity)
    account = await account_crud.get_account(db, account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="계정을 찾을 수 없습니다.")

    results = await group_search_crud.get_recent_results(db, account_id, keyword=keyword)
    total = await group_search_crud.count_search_results(db, account_id, keyword=keyword)
    return GroupSearchResultList(items=results, total=total, keyword=keyword or "")


@router.post("/join", response_model=list[dict])
async def join_groups(
    payload: JoinGroupRequest,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Join selected groups from search results.

    Enforces daily join limit (MAX_DAILY_JOINS per account).
    """
    rows = await group_search_crud.get_results_by_ids(db, payload.result_ids)
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="선택된 결과가 없습니다.")

    account_id = rows[0].account_id
    await require_account_tenant_access(account_id, db, identity)
    account = await account_crud.get_account(db, account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="계정을 찾을 수 없습니다.")

    try:
        results = await join_selected_groups(account, payload.result_ids)
    except DailyJoinLimitExceededError as exc:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc))
    except Exception as exc:
        logger.error("join_groups_failed", account_id=account.id, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"그룹 가입에 실패했습니다: {exc}",
        )

    return results


@router.get("/join-info/{account_id}", response_model=JoinInfo)
async def get_join_info(
    account_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Get daily join usage info for an account."""
    await require_account_tenant_access(account_id, db, identity)
    account = await account_crud.get_account(db, account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="계정을 찾을 수 없습니다.")

    joined_today = await group_search_crud.count_today_joins(db, account_id)
    return JoinInfo(
        joined_today=joined_today,
        max_daily=MAX_DAILY_JOINS,
        remaining=max(0, MAX_DAILY_JOINS - joined_today),
    )


@router.get("/join-stats/{account_id}", response_model=GroupJoinStats)
async def get_join_stats(
    account_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Get aggregated join statistics for an account."""
    await require_account_tenant_access(account_id, db, identity)
    account = await account_crud.get_account(db, account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="계정을 찾을 수 없습니다.")

    stats = await group_search_crud.get_join_stats(db, account_id)
    return GroupJoinStats(**stats)


@router.get("/join-logs/{account_id}", response_model=GroupJoinLogList)
async def get_join_logs(
    account_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Get paginated join history for an account."""
    await require_account_tenant_access(account_id, db, identity)
    account = await account_crud.get_account(db, account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="계정을 찾을 수 없습니다.")

    offset = (page - 1) * page_size
    logs = await group_search_crud.get_join_logs(db, account_id, limit=page_size, offset=offset)
    total = await group_search_crud.count_join_logs(db, account_id)
    total_pages = max(1, (total + page_size - 1) // page_size)

    return GroupJoinLogList(
        items=logs,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )
