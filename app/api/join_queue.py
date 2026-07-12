"""Smart Join Queue API — manage the join queue and its configuration.

Integrates with Bulk Link Inspector: after inspecting links, users can add
active links to the queue, view queue status, configure join rate, and
pause/resume processing.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_identity, Identity, require_account_tenant_access
from app.core.logging import get_logger
from app.crud import account as account_crud
from app.crud import join_queue as queue_crud
from app.database import get_db
from app.schemas.join_queue import (
    AddToQueueRequest,
    AddToQueueResponse,
    ClearQueueRequest,
    ClearQueueResponse,
    PaginatedQueueItems,
    QueueConfigRead,
    QueueConfigUpdate,
    QueueItemRead,
    QueueStats,
    RemoveFromQueueRequest,
    RemoveFromQueueResponse,
)

router = APIRouter(prefix="/api/join-queue", tags=["join-queue"])
logger = get_logger(__name__)


@router.post("/add", response_model=AddToQueueResponse, status_code=status.HTTP_201_CREATED)
async def add_to_queue(
    payload: AddToQueueRequest,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Add inspected links to the join queue for sequential processing."""
    await require_account_tenant_access(payload.account_id, db, identity)
    account = await account_crud.get_account(db, payload.account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="계정을 찾을 수 없습니다.")

    items_data = [
        {
            "raw_link": item.raw_link,
            "title": item.title,
            "chat_type": item.chat_type,
            "username": item.username,
            "chat_id": item.chat_id,
            "delay_before_seconds": item.delay_before_seconds,
        }
        for item in payload.items
    ]

    created = await queue_crud.add_many_to_queue(db, payload.account_id, items_data)
    read_items = [QueueItemRead.model_validate(item) for item in created]

    logger.info(
        "queue_items_added",
        account_id=payload.account_id,
        count=len(created),
        user_id=identity.user_id,
    )

    return AddToQueueResponse(items=read_items, total_added=len(created))


@router.get("/{account_id}", response_model=PaginatedQueueItems)
async def list_queue(
    account_id: str,
    status: str | None = Query(default=None, description="Filter by status: queued, processing, success, failed, flood_wait"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """List queue items for an account with pagination and optional status filter."""
    await require_account_tenant_access(account_id, db, identity)
    account = await account_crud.get_account(db, account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="계정을 찾을 수 없습니다.")

    offset = (page - 1) * page_size
    items, total = await queue_crud.list_queue(
        db, account_id, status_filter=status, limit=page_size, offset=offset
    )
    total_pages = max(1, (total + page_size - 1) // page_size)

    return PaginatedQueueItems(
        items=[QueueItemRead.model_validate(item) for item in items],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )


@router.delete("/{account_id}/items", response_model=RemoveFromQueueResponse)
async def remove_items(
    account_id: str,
    payload: RemoveFromQueueRequest,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Remove specific items from the queue."""
    await require_account_tenant_access(account_id, db, identity)

    removed = 0
    for item_id in payload.item_ids:
        if await queue_crud.remove_from_queue(db, item_id):
            removed += 1

    logger.info(
        "queue_items_removed",
        account_id=account_id,
        count=removed,
        user_id=identity.user_id,
    )
    return RemoveFromQueueResponse(removed_count=removed)


@router.post("/{account_id}/clear", response_model=ClearQueueResponse)
async def clear_queue(
    account_id: str,
    payload: ClearQueueRequest,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Clear all items (optionally filtered by status) from the queue."""
    await require_account_tenant_access(account_id, db, identity)

    cleared = await queue_crud.clear_queue(db, account_id, status=payload.status)
    logger.info(
        "queue_cleared",
        account_id=account_id,
        count=cleared,
        status_filter=payload.status,
        user_id=identity.user_id,
    )
    return ClearQueueResponse(cleared_count=cleared)


@router.get("/{account_id}/config", response_model=QueueConfigRead)
async def get_config(
    account_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Get the join queue configuration for an account."""
    await require_account_tenant_access(account_id, db, identity)
    config = await queue_crud.get_or_create_config(db, account_id)
    return QueueConfigRead.model_validate(config)


@router.put("/{account_id}/config", response_model=QueueConfigRead)
async def update_config(
    account_id: str,
    payload: QueueConfigUpdate,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Update the join queue configuration (pause/resume, rate, daily limit)."""
    await require_account_tenant_access(account_id, db, identity)
    config = await queue_crud.update_config(
        db,
        account_id,
        is_paused=payload.is_paused,
        joins_per_hour=payload.joins_per_hour,
        max_daily_joins=payload.max_daily_joins,
    )
    logger.info(
        "queue_config_updated",
        account_id=account_id,
        is_paused=config.is_paused,
        joins_per_hour=config.joins_per_hour,
        max_daily_joins=config.max_daily_joins,
        user_id=identity.user_id,
    )
    return QueueConfigRead.model_validate(config)


@router.get("/{account_id}/stats", response_model=QueueStats)
async def get_stats(
    account_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Get queue statistics for an account."""
    await require_account_tenant_access(account_id, db, identity)

    config = await queue_crud.get_or_create_config(db, account_id)
    joined_today = await queue_crud.count_today_joins(db, account_id)

    # Count by status
    from sqlalchemy import func as sa_func, and_
    from app.models.join_queue import JoinQueueItem

    status_counts = {}
    for status_val in ("queued", "processing", "success", "failed", "flood_wait"):
        result = await db.execute(
            sa_func.count(JoinQueueItem.id).where(
                and_(
                    JoinQueueItem.account_id == account_id,
                    JoinQueueItem.status == status_val,
                )
            )
        )
        status_counts[status_val] = result.scalar()

    return QueueStats(
        account_id=account_id,
        total_queued=status_counts.get("queued", 0),
        total_processing=status_counts.get("processing", 0),
        total_success=status_counts.get("success", 0),
        total_failed=status_counts.get("failed", 0),
        total_flood_wait=status_counts.get("flood_wait", 0),
        joined_today=joined_today,
        max_daily_joins=config.max_daily_joins,
        is_paused=config.is_paused,
        joins_per_hour=config.joins_per_hour,
    )