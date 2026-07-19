from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_identity, Identity, require_account_tenant_access
from app.core.logging import get_logger
from app.crud import account as account_crud
from app.crud import group_search as group_search_crud
from app.crud import join_queue as queue_crud
from app.database import get_db
from app.schemas.group_search import (
    AutoQueueRequest,
    AutoQueueResponse,
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

router = APIRouter(prefix="/api/group-search", tags=["group-search"])
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


@router.post("/auto-queue", response_model=AutoQueueResponse)
async def auto_queue_groups(
    payload: AutoQueueRequest,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """검색 결과 중 조건(최소 인원 수, 미가입, 공개 username 존재)을 만족하는 그룹을
    Smart Join Queue에 자동 등록한다. 실제 입장은 큐 프로세서(스케줄러)가
    안전한 속도로 순차 처리한다 — 즉시 대량 가입으로 인한 계정 제재를 피하기 위함."""
    await require_account_tenant_access(payload.account_id, db, identity)
    account = await account_crud.get_account(db, payload.account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="계정을 찾을 수 없습니다.")

    results = await group_search_crud.get_recent_results(db, payload.account_id, keyword=payload.keyword, limit=200)
    existing_items, _ = await queue_crud.list_queue(db, payload.account_id, limit=1000)
    already_queued = {item.chat_id for item in existing_items if item.chat_id} | {
        item.username for item in existing_items if item.username
    }

    to_queue: list[dict] = []
    skipped_already_joined = 0
    skipped_already_queued = 0
    skipped_below_threshold = 0
    skipped_no_username = 0

    for r in results:
        if r.is_joined:
            skipped_already_joined += 1
            continue
        if r.chat_id in already_queued or (r.username and r.username in already_queued):
            skipped_already_queued += 1
            continue
        if (r.participants_count or 0) < payload.min_members:
            skipped_below_threshold += 1
            continue
        if not r.username:
            skipped_no_username += 1
            continue
        to_queue.append({
            "raw_link": f"https://t.me/{r.username}",
            "title": r.title,
            "chat_type": r.chat_type,
            "username": r.username,
            "chat_id": r.chat_id,
        })

    if to_queue:
        await queue_crud.add_many_to_queue(db, payload.account_id, to_queue)
        logger.info("group_search_auto_queued", account_id=payload.account_id, count=len(to_queue))

    return AutoQueueResponse(
        queued=len(to_queue),
        skipped_already_joined=skipped_already_joined,
        skipped_already_queued=skipped_already_queued,
        skipped_below_threshold=skipped_below_threshold,
        skipped_no_username=skipped_no_username,
    )


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
