from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.limits import BROADCAST_MIN_INTERVAL_SECONDS
from app.models.broadcast import Broadcast
from app.schemas.broadcast import BroadcastCreate


def utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def create_broadcast(
    db: AsyncSession, data: BroadcastCreate, media_path: str | None, *, scheduled_at: datetime | None
) -> Broadcast:
    broadcast = Broadcast(
        account_id=data.account_id,
        message=data.message,
        recipients=data.recipients,
        media_path=media_path,
        status="pending",
        scheduled_at=scheduled_at,
    )
    db.add(broadcast)
    await db.commit()
    await db.refresh(broadcast)
    return broadcast


async def get_broadcast(db: AsyncSession, broadcast_id: str) -> Broadcast | None:
    return await db.get(Broadcast, broadcast_id)


async def seconds_until_next_allowed_broadcast(
    db: AsyncSession, account_id: str, *, exclude_id: str | None = None
) -> float:
    """Returns 0 if a broadcast may fire now, otherwise the seconds to wait.

    Looks at every broadcast for this account that is due "now or earlier" (i.e. not one
    scheduled for later), using sent_at when set (actual send-attempt time) and falling
    back to created_at for ones that haven't started yet. Status is deliberately NOT
    filtered here — a broadcast that failed still reached the "sending" stage and got
    sent_at stamped, so it still counts against the cooldown; only an in-progress attempt
    that hasn't reached "sending" yet has no sent_at and falls back to created_at.
    Checked both when a broadcast is created (immediate sends) and again right before a
    worker actually executes one — the second check is what keeps the cap correct once
    a queue/scheduler can dispatch multiple jobs close together.
    """
    now = utcnow_naive()
    reference_time = func.coalesce(Broadcast.sent_at, Broadcast.created_at)
    query = (
        select(reference_time)
        .where(
            Broadcast.account_id == account_id,
            (Broadcast.scheduled_at.is_(None)) | (Broadcast.scheduled_at <= now),
        )
        .order_by(reference_time.desc())
        .limit(1)
    )
    if exclude_id:
        query = query.where(Broadcast.id != exclude_id)

    result = await db.execute(query)
    last_time = result.scalar_one_or_none()
    if last_time is None:
        return 0
    elapsed = (now - last_time).total_seconds()
    return max(0.0, BROADCAST_MIN_INTERVAL_SECONDS - elapsed)


async def update_broadcast_status(
    db: AsyncSession,
    broadcast: Broadcast,
    *,
    status: str,
    error_message: str | None = None,
    mark_sent: bool = False,
) -> Broadcast:
    broadcast.status = status
    broadcast.error_message = error_message
    if mark_sent:
        broadcast.sent_at = utcnow_naive()
    await db.commit()
    await db.refresh(broadcast)
    return broadcast


async def list_due_scheduled_broadcasts(db: AsyncSession) -> list[Broadcast]:
    now = utcnow_naive()
    result = await db.execute(
        select(Broadcast).where(
            Broadcast.status == "pending",
            Broadcast.scheduled_at.is_not(None),
            Broadcast.scheduled_at <= now,
        )
    )
    return list(result.scalars().all())


async def list_upcoming_scheduled_broadcasts(db: AsyncSession) -> list[Broadcast]:
    now = utcnow_naive()
    result = await db.execute(
        select(Broadcast)
        .where(
            Broadcast.status == "pending",
            Broadcast.scheduled_at.is_not(None),
            Broadcast.scheduled_at > now,
        )
        .order_by(Broadcast.scheduled_at.asc())
    )
    return list(result.scalars().all())


async def list_logs(
    db: AsyncSession,
    *,
    account_id: str | None = None,
    status: str | None = None,
    date: str | None = None,
) -> list[Broadcast]:
    query = select(Broadcast).order_by(Broadcast.created_at.desc())
    if account_id:
        query = query.where(Broadcast.account_id == account_id)
    if status:
        query = query.where(Broadcast.status == status)
    if date:
        day_start = datetime.strptime(date, "%Y-%m-%d")
        day_end = day_start + timedelta(days=1)
        query = query.where(Broadcast.created_at >= day_start, Broadcast.created_at < day_end)
    result = await db.execute(query)
    return list(result.scalars().all())
