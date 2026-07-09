"""Delivery Analytics API — tenant-safe operational metrics from MessageLog."""

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import get_current_identity, Identity
from app.services.delivery_analytics import (
    get_summary,
    get_failure_breakdown,
    get_account_performance,
    get_timeline,
    get_recent_activity,
)

router = APIRouter(prefix="/api/delivery-analytics", tags=["delivery-analytics"])


@router.get("/summary")
async def api_summary(
    account_id: str | None = None,
    days: int = Query(default=30, le=365, ge=1),
    identity: Identity = Depends(get_current_identity),
):
    """Get delivery summary for authorized accounts."""
    result = await get_summary(identity, account_id=account_id, days=days)
    return result


@router.get("/failures")
async def api_failures(
    account_id: str | None = None,
    days: int = Query(default=30, le=365, ge=1),
    identity: Identity = Depends(get_current_identity),
):
    """Get failure breakdown by category."""
    result = await get_failure_breakdown(identity, account_id=account_id, days=days)
    return result


@router.get("/accounts")
async def api_account_performance(
    days: int = Query(default=30, le=365, ge=1),
    identity: Identity = Depends(get_current_identity),
):
    """Get delivery performance per authorized account."""
    result = await get_account_performance(identity, days=days)
    return result


@router.get("/timeline")
async def api_timeline(
    account_id: str | None = None,
    days: int = Query(default=30, le=365, ge=1),
    interval: str = Query(default="day", pattern="^(hour|day)$"),
    identity: Identity = Depends(get_current_identity),
):
    """Get delivery timeline grouped by hour or day."""
    result = await get_timeline(identity, account_id=account_id, days=days, interval=interval)
    return result


@router.get("/recent")
async def api_recent(
    account_id: str | None = None,
    limit: int = Query(default=50, le=200, ge=1),
    identity: Identity = Depends(get_current_identity),
):
    """Get most recent delivery activity. Safe fields only — no secrets."""
    result = await get_recent_activity(identity, account_id=account_id, limit=limit)
    return result