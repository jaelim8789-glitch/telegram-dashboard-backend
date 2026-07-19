"""AI Events API — manage event subscriptions and publish events."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.models.ai_event import AiEventSubscription, AiEventLog
from app.ai.schemas.ai_event import (
    EventSubscriptionCreate,
    EventSubscriptionUpdate,
    EventSubscriptionResponse,
    EventPublishRequest,
    EventLogResponse,
    EventBusStats,
)
from app.ai.event_bus.bus import get_event_bus
from app.api.deps import get_current_tenant_id
from app.database import get_db

router = APIRouter(prefix="/ai/events", tags=["AI Events"])


@router.post("/publish")
async def publish_event(
    request: EventPublishRequest,
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
):
    bus = get_event_bus()
    handler_count = await bus.publish(
        request.event_type,
        request.payload,
        source=request.source,
        correlation_id=request.correlation_id,
        db=db,
        tenant_id=tenant_id,
    )
    return {"event_type": request.event_type, "handler_count": handler_count}


@router.get("/subscriptions", response_model=list[EventSubscriptionResponse])
async def list_subscriptions(
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(AiEventSubscription).where(AiEventSubscription.tenant_id == tenant_id)
    )
    subs = result.scalars().all()
    return [EventSubscriptionResponse.model_validate(s) for s in subs]


@router.post("/subscriptions", response_model=EventSubscriptionResponse, status_code=201)
async def create_subscription(
    data: EventSubscriptionCreate,
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
):
    import uuid
    sub = AiEventSubscription(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        **data.model_dump(),
    )
    db.add(sub)
    await db.commit()
    await db.refresh(sub)
    return EventSubscriptionResponse.model_validate(sub)


@router.put("/subscriptions/{sub_id}", response_model=EventSubscriptionResponse)
async def update_subscription(
    sub_id: str,
    data: EventSubscriptionUpdate,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(AiEventSubscription).where(AiEventSubscription.id == sub_id)
    )
    sub = result.scalar_one_or_none()
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")
    for key, value in data.model_dump(exclude_none=True).items():
        if hasattr(sub, key) and value is not None:
            setattr(sub, key, value)
    await db.commit()
    await db.refresh(sub)
    return EventSubscriptionResponse.model_validate(sub)


@router.delete("/subscriptions/{sub_id}", status_code=204)
async def delete_subscription(
    sub_id: str,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(AiEventSubscription).where(AiEventSubscription.id == sub_id)
    )
    sub = result.scalar_one_or_none()
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")
    await db.delete(sub)
    await db.commit()


@router.get("/logs", response_model=list[EventLogResponse])
async def list_event_logs(
    event_type: str | None = None,
    limit: int = 50,
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
):
    query = select(AiEventLog).where(AiEventLog.tenant_id == tenant_id)
    if event_type:
        query = query.where(AiEventLog.event_type == event_type)
    query = query.order_by(AiEventLog.created_at.desc()).limit(limit)
    result = await db.execute(query)
    logs = result.scalars().all()
    return [EventLogResponse.model_validate(l) for l in logs]


@router.get("/stats", response_model=EventBusStats)
async def get_event_stats(
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
):
    from datetime import datetime, timedelta, timezone
    since = datetime.now(timezone.utc) - timedelta(hours=24)

    # Events in last 24h
    result = await db.execute(
        select(func.count(AiEventLog.id)).where(
            AiEventLog.tenant_id == tenant_id,
            AiEventLog.created_at >= since,
        )
    )
    total_events = result.scalar() or 0

    # Events by type
    type_result = await db.execute(
        select(AiEventLog.event_type, func.count(AiEventLog.id)).where(
            AiEventLog.tenant_id == tenant_id,
            AiEventLog.created_at >= since,
        ).group_by(AiEventLog.event_type)
    )
    events_by_type = {row.event_type: row[1] for row in type_result}

    # Failure rate
    fail_result = await db.execute(
        select(func.count(AiEventLog.id)).where(
            AiEventLog.tenant_id == tenant_id,
            AiEventLog.created_at >= since,
            AiEventLog.handler_failure_count > 0,
        )
    )
    failures = fail_result.scalar() or 0
    failure_rate = (failures / total_events * 100) if total_events > 0 else 0.0

    # Subscription counts
    sub_result = await db.execute(
        select(func.count(AiEventSubscription.id)).where(
            AiEventSubscription.tenant_id == tenant_id,
        )
    )
    total_subs = sub_result.scalar() or 0

    active_result = await db.execute(
        select(func.count(AiEventSubscription.id)).where(
            AiEventSubscription.tenant_id == tenant_id,
            AiEventSubscription.is_active == True,
        )
    )
    active_subs = active_result.scalar() or 0

    return EventBusStats(
        total_events_24h=total_events,
        total_subscriptions=total_subs,
        active_subscriptions=active_subs,
        events_by_type=events_by_type,
        failure_rate_24h=round(failure_rate, 2),
    )