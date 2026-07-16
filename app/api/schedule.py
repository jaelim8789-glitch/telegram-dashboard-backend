"""Calendar schedule view for broadcasts."""

from datetime import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_identity, Identity
from app.core.logging import get_logger
from app.crud import schedule as schedule_crud
from app.database import get_db
from app.crud import broadcast as broadcast_crud

router = APIRouter(tags=["schedule"])
logger = get_logger(__name__)


@router.get("/api/schedule/calendar")
async def get_calendar(
    start: str = Query(..., description="ISO 8601 start"),
    end: str = Query(..., description="ISO 8601 end"),
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    try:
        start_dt = dt.fromisoformat(start)
        end_dt = dt.fromisoformat(end)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date format. Use ISO 8601.")

    if identity.kind == "admin" and not identity.tenant_id:
        entries = await schedule_crud.get_all_schedule_entries(db, start_dt, end_dt)
    else:
        entries = await schedule_crud.get_schedule_entries(db, identity.tenant_id or "", start_dt, end_dt)

    return [
        {
            "id": e.id,
            "title": e.title,
            "scheduled_at": e.scheduled_at.isoformat() if e.scheduled_at else None,
            "status": e.status,
            "broadcast_id": e.broadcast_id,
            "campaign_id": e.campaign_id,
        }
        for e in entries
    ]


@router.post("/api/schedule/sync")
async def sync_schedule(
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    from app.crud import broadcast as broadcast_crud
    broadcasts = await broadcast_crud.list_upcoming_scheduled_broadcasts(db, identity=identity)
    count = 0
    for b in broadcasts:
        tenant_id = identity.tenant_id or "default"
        if b.scheduled_at:
            await schedule_crud.sync_broadcast_to_schedule(
                db, tenant_id, b.id, b.message[:80], b.scheduled_at, b.status
            )
            count += 1
    return {"synced": count}
