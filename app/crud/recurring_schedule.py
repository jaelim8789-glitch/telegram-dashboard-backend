from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.recurring_schedule import RecurringSchedule
from app.schemas.recurring_schedule import RecurringScheduleCreate


def utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def create_recurring_schedule(
    db: AsyncSession, data: RecurringScheduleCreate, media_path: str | None
) -> RecurringSchedule:
    schedule = RecurringSchedule(
        account_id=data.account_id,
        message=data.message,
        media_path=media_path,
        interval_minutes=data.interval_minutes,
        group_ids=data.group_ids,
        is_active=True,
        total_sends=0,
    )
    db.add(schedule)
    await db.commit()
    await db.refresh(schedule)
    return schedule


async def get_recurring_schedule(db: AsyncSession, schedule_id: str) -> RecurringSchedule | None:
    return await db.get(RecurringSchedule, schedule_id)


async def list_active_recurring_schedules(db: AsyncSession) -> list[RecurringSchedule]:
    result = await db.execute(
        select(RecurringSchedule).where(RecurringSchedule.is_active == True)  # noqa: E712
    )
    return list(result.scalars().all())


async def list_recurring_schedules(db: AsyncSession, account_id: str | None = None) -> list[RecurringSchedule]:
    query = select(RecurringSchedule).order_by(RecurringSchedule.created_at.desc())
    if account_id:
        query = query.where(RecurringSchedule.account_id == account_id)
    result = await db.execute(query)
    return list(result.scalars().all())


async def update_schedule_status(db: AsyncSession, schedule_id: str, is_active: bool) -> RecurringSchedule | None:
    schedule = await get_recurring_schedule(db, schedule_id)
    if schedule is None:
        return None
    schedule.is_active = is_active
    await db.commit()
    await db.refresh(schedule)
    return schedule


async def increment_send_count(db: AsyncSession, schedule_id: str) -> None:
    now = utcnow_naive()
    await db.execute(
        update(RecurringSchedule)
        .where(RecurringSchedule.id == schedule_id)
        .values(total_sends=RecurringSchedule.total_sends + 1, last_sent_at=now)
    )
    await db.commit()


async def delete_recurring_schedule(db: AsyncSession, schedule_id: str) -> bool:
    schedule = await get_recurring_schedule(db, schedule_id)
    if schedule is None:
        return False
    await db.delete(schedule)
    await db.commit()
    return True
