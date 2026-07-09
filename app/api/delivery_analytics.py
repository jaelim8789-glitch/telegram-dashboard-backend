"""Delivery Analytics API — tenant-safe operational metrics from MessageLog.

Sprint 16 extensions:
- Optional filtering (source, account_id, status, start_time, end_time)
- Source analytics (GET /source)
- Broadcast analytics (GET /broadcasts)
- Failure intelligence (GET /failures/intelligence)
- Overview endpoint (GET /overview)
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import get_current_identity, Identity
from app.services.delivery_analytics import (
    get_summary,
    get_failure_breakdown,
    get_account_performance,
    get_timeline,
    get_recent_activity,
    get_source_analytics,
    get_broadcast_analytics,
    get_failure_intelligence,
    get_logical_summary,
    get_logical_broadcast_analytics,
    get_overview,
)

router = APIRouter(prefix="/api/delivery-analytics", tags=["delivery-analytics"])


@router.get("/summary")
async def api_summary(
    account_id: str | None = None,
    days: int = Query(default=30, le=365, ge=1),
    source: str | None = None,
    status: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    identity: Identity = Depends(get_current_identity),
):
    """Get delivery summary for authorized accounts.

    Optional filters: source, status, start_time, end_time (ISO datetime).
    All filters preserve tenant isolation.
    """
    result = await get_summary(
        identity, account_id=account_id, days=days,
        source=source, status=status,
        start_time=start_time, end_time=end_time,
    )
    return result


@router.get("/failures")
async def api_failures(
    account_id: str | None = None,
    days: int = Query(default=30, le=365, ge=1),
    source: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    identity: Identity = Depends(get_current_identity),
):
    """Get failure breakdown by category.

    Optional filters: source, start_time, end_time (ISO datetime).
    """
    result = await get_failure_breakdown(
        identity, account_id=account_id, days=days,
        source=source,
        start_time=start_time, end_time=end_time,
    )
    return result


@router.get("/accounts")
async def api_account_performance(
    days: int = Query(default=30, le=365, ge=1),
    source: str | None = None,
    status: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    identity: Identity = Depends(get_current_identity),
):
    """Get delivery performance per authorized account.

    Optional filters: source, status, start_time, end_time (ISO datetime).
    """
    result = await get_account_performance(
        identity, days=days,
        source=source, status=status,
        start_time=start_time, end_time=end_time,
    )
    return result


@router.get("/timeline")
async def api_timeline(
    account_id: str | None = None,
    days: int = Query(default=30, le=365, ge=1),
    interval: str = Query(default="day", pattern="^(hour|day)$"),
    source: str | None = None,
    status: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    identity: Identity = Depends(get_current_identity),
):
    """Get delivery timeline grouped by hour or day.

    Optional filters: source, status, start_time, end_time (ISO datetime).
    """
    result = await get_timeline(
        identity, account_id=account_id, days=days, interval=interval,
        source=source, status=status,
        start_time=start_time, end_time=end_time,
    )
    return result


@router.get("/recent")
async def api_recent(
    account_id: str | None = None,
    limit: int = Query(default=50, le=200, ge=1),
    source: str | None = None,
    status: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    identity: Identity = Depends(get_current_identity),
):
    """Get most recent delivery activity. Safe fields only — no secrets.

    Optional filters: source, status, start_time, end_time (ISO datetime).
    """
    result = await get_recent_activity(
        identity, account_id=account_id, limit=limit,
        source=source, status=status,
        start_time=start_time, end_time=end_time,
    )
    return result


@router.get("/source")
async def api_source_analytics(
    account_id: str | None = None,
    days: int = Query(default=30, le=365, ge=1),
    start_time: str | None = None,
    end_time: str | None = None,
    identity: Identity = Depends(get_current_identity),
):
    """Get delivery analytics grouped by source.

    Sources derived from actual persisted data: broadcast, reply_macro, manual, scheduled.
    Attempt-level counts (retries included).
    """
    result = await get_source_analytics(
        identity, account_id=account_id, days=days,
        start_time=start_time, end_time=end_time,
    )
    return result


@router.get("/broadcasts")
async def api_broadcast_analytics(
    account_id: str | None = None,
    days: int = Query(default=30, le=365, ge=1),
    start_time: str | None = None,
    end_time: str | None = None,
    identity: Identity = Depends(get_current_identity),
):
    """Get per-broadcast delivery analytics.

    Correlates MessageLog records via source='broadcast' and source_id=<broadcast.id>.
    Attempt-level counts within each broadcast. Only broadcasts with persisted
    MessageLog records are included.
    """
    result = await get_broadcast_analytics(
        identity, account_id=account_id, days=days,
        start_time=start_time, end_time=end_time,
    )
    return result


@router.get("/failures/intelligence")
async def api_failure_intelligence(
    account_id: str | None = None,
    days: int = Query(default=30, le=365, ge=1),
    source: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    identity: Identity = Depends(get_current_identity),
):
    """Enhanced failure analytics with percentages, affected accounts, and latest occurrence.

    Never exposes raw exceptions, API keys, Telegram session secrets, or credentials.
    Only uses the safe error_message field which is already sanitized.
    """
    result = await get_failure_intelligence(
        identity, account_id=account_id, days=days,
        source=source,
        start_time=start_time, end_time=end_time,
    )
    return result


@router.get("/overview")
async def api_overview(
    account_id: str | None = None,
    days: int = Query(default=30, le=365, ge=1),
    start_time: str | None = None,
    end_time: str | None = None,
    identity: Identity = Depends(get_current_identity),
):
    """Single aggregated analytics overview.

    Combines summary, source breakdown, top accounts, failure intelligence,
    and timeline into one response. Reuses existing service functions.
    Response is bounded by design.
    """
    result = await get_overview(
        identity, account_id=account_id, days=days,
        start_time=start_time, end_time=end_time,
    )
    return result


@router.get("/logical/summary")
async def api_logical_summary(
    account_id: str | None = None,
    days: int = Query(default=30, le=365, ge=1),
    source: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    identity: Identity = Depends(get_current_identity),
):
    """Get logical-delivery-level summary.

    Groups by (account_id, source, source_id, recipient). Retries are
    collapsed into one outcome per recipient. total_recipients counts
    distinct recipients, not attempts.
    """
    result = await get_logical_summary(
        identity, account_id=account_id, days=days,
        source=source,
        start_time=start_time, end_time=end_time,
    )
    return result


@router.get("/logical/broadcasts")
async def api_logical_broadcast_analytics(
    account_id: str | None = None,
    days: int = Query(default=30, le=365, ge=1),
    start_time: str | None = None,
    end_time: str | None = None,
    identity: Identity = Depends(get_current_identity),
):
    """Get per-broadcast logical delivery analytics.

    Retries within a broadcast are collapsed into one outcome per recipient.
    total_recipients counts distinct recipients, not attempts.
    """
    result = await get_logical_broadcast_analytics(
        identity, account_id=account_id, days=days,
        start_time=start_time, end_time=end_time,
    )
    return result
