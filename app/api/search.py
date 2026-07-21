"""Delivery history search endpoint."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_identity, Identity
from app.core.logging import get_logger
from app.crud import broadcast as broadcast_crud
from app.database import get_db
from app.services.failure_intel import classify_failure
from app.schemas.search import BroadcastSearchItem, BroadcastSearchResponse

router = APIRouter(tags=["search"])
logger = get_logger(__name__)


@router.get("/api/broadcast/search", response_model=BroadcastSearchResponse)
async def search_broadcasts(
    account_id: str | None = Query(None),
    status: str | None = Query(None, description="Comma-separated: pending,sending,sent,failed,cancelled"),
    q: str | None = Query(None, alias="q", description="Search in message content"),
    date_from: str | None = Query(None, description="YYYY-MM-DD"),
    date_to: str | None = Query(None, description="YYYY-MM-DD"),
    campaign_id: str | None = Query(None),
    sort_by: str = Query("created_at"),
    sort_order: str = Query("desc"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    skip = (page - 1) * page_size

    items, total = await broadcast_crud.search_broadcasts(
        db,
        identity=identity,
        account_id=account_id,
        status=status,
        message_search=q,
        date_from=date_from,
        date_to=date_to,
        campaign_id=campaign_id,
        sort_by=sort_by,
        sort_order=sort_order,
        skip=skip,
        limit=page_size,
    )

    enriched = []
    for b in items:
        item_data = dict(
            id=b.id,
            account_id=b.account_id,
            message=b.message[:200],
            status=b.status,
            scheduled_at=b.scheduled_at.isoformat() if b.scheduled_at else None,
            sent_at=b.sent_at.isoformat() if b.sent_at else None,
            created_at=b.created_at.isoformat() if b.created_at else None,
            error_message=b.error_message,
            retry_count=b.retry_count or 0,
            recipient_count=len(b.recipients) if b.recipients else 0,
            delivery_mode=b.delivery_mode,
            campaign_id=b.campaign_id,
            is_recurring=b.recurring_interval_minutes is not None,
        )
        if b.status == "failed" and b.error_message:
            item_data["failure_info"] = classify_failure(b.status, b.error_message)
        enriched.append(BroadcastSearchItem(**item_data))

    return BroadcastSearchResponse(
        items=enriched,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=max(1, (total + page_size - 1) // page_size),
    )
