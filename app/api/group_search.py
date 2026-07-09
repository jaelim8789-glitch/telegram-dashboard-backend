from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.crud import account as account_crud
from app.crud import group_search as group_search_crud
from app.database import get_db
from app.schemas.group_search import (
    GroupJoinLogRead,
    GroupSearchRequest,
    GroupSearchResultRead,
    JoinGroupRequest,
    JoinInfo,
)
from app.services.group_search_service import DailyJoinLimitExceededError, search_public_groups, join_selected_groups
from app.core.limits import MAX_DAILY_JOINS

router = APIRouter(prefix="/api/group-search", tags=["group-search"])
logger = get_logger(__name__)


@router.post("/search", response_model=list[GroupSearchResultRead])
async def search_groups(payload: GroupSearchRequest, db: AsyncSession = Depends(get_db)):
    """Search Telegram for public groups matching a keyword and store results."""
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

    # Return from DB so we have the persisted IDs
    saved = await group_search_crud.get_recent_results(db, account.id, keyword=payload.keyword)
    return saved[:100]


@router.get("/results/{account_id}", response_model=list[GroupSearchResultRead])
async def get_search_results(account_id: str, db: AsyncSession = Depends(get_db)):
    """Get recent search results for an account."""
    account = await account_crud.get_account(db, account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="계정을 찾을 수 없습니다.")
    return await group_search_crud.get_recent_results(db, account_id)


@router.post("/join", response_model=list[dict])
async def join_groups(payload: JoinGroupRequest, db: AsyncSession = Depends(get_db)):
    """Join selected groups from search results.

    Enforces daily join limit (MAX_DAILY_JOINS per account).
    """
    # Get the first result to determine account_id
    rows = await group_search_crud.get_results_by_ids(db, payload.result_ids)
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="선택된 결과가 없습니다.")

    account_id = rows[0].account_id
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
async def get_join_info(account_id: str, db: AsyncSession = Depends(get_db)):
    """Get daily join usage info for an account."""
    account = await account_crud.get_account(db, account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="계정을 찾을 수 없습니다.")

    joined_today = await group_search_crud.count_today_joins(db, account_id)
    return JoinInfo(
        joined_today=joined_today,
        max_daily=MAX_DAILY_JOINS,
        remaining=max(0, MAX_DAILY_JOINS - joined_today),
    )


@router.get("/join-logs/{account_id}", response_model=list[GroupJoinLogRead])
async def get_join_logs(account_id: str, db: AsyncSession = Depends(get_db)):
    """Get join history for an account."""
    account = await account_crud.get_account(db, account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="계정을 찾을 수 없습니다.")
    return await group_search_crud.get_join_logs(db, account_id)
