"""CRUD for BroadcastScheduleEntry (calendar view)."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.schedule import BroadcastScheduleEntry


async def get_schedule_entries(
    db: AsyncSession, tenant_id: str, start: datetime, end: datetime
) -> list[BroadcastScheduleEntry]:
    result = await db.execute(
        select(BroadcastScheduleEntry).where(
            BroadcastScheduleEntry.tenant_id == tenant_id,
            BroadcastScheduleEntry.scheduled_at >= start,
            BroadcastScheduleEntry.scheduled_at <= end,
        ).order_by(BroadcastScheduleEntry.scheduled_at.asc())
    )
    return list(result.scalars().all())


async def get_all_schedule_entries(
    db: AsyncSession, start: datetime, end: datetime
) -> list[BroadcastScheduleEntry]:
    result = await db.execute(
        select(BroadcastScheduleEntry).where(
            BroadcastScheduleEntry.scheduled_at >= start,
            BroadcastScheduleEntry.scheduled_at <= end,
        ).order_by(BroadcastScheduleEntry.scheduled_at.asc())
    )
    return list(result.scalars().all())


async def sync_broadcast_to_schedule(
    db: AsyncSession, tenant_id: str, broadcast_id: str, title: str, scheduled_at: datetime, status: str
) -> None:
    from app.models.schedule import BroadcastScheduleEntry
    existing = await db.execute(
        select(BroadcastScheduleEntry).where(
            BroadcastScheduleEntry.broadcast_id == broadcast_id
        ).limit(1)
    )
    if existing.scalar_one_or_none() is not None:
        return
    entry = BroadcastScheduleEntry(
        tenant_id=tenant_id,
        broadcast_id=broadcast_id,
        title=title,
        scheduled_at=scheduled_at,
        status=status,
    )
    db.add(entry)
    await db.commit()
