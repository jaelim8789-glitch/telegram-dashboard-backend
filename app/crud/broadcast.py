from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.limits import BROADCAST_MIN_INTERVAL_SECONDS
from app.models.broadcast import Broadcast
from app.schemas.broadcast import BroadcastCreate, RECURRING_INTERVAL_VALUES


def utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def create_broadcast(
    db: AsyncSession, data: BroadcastCreate, media_path: str | None, *, scheduled_at: datetime | None
) -> Broadcast:
    # Validate recurring interval
    if data.recurring_interval_minutes is not None:
        if data.recurring_interval_minutes not in RECURRING_INTERVAL_VALUES:
            raise ValueError(
                f"recurring_interval_minutes must be one of {sorted(RECURRING_INTERVAL_VALUES)}, "
                f"got {data.recurring_interval_minutes}"
            )

    now = utcnow_naive()
    broadcast = Broadcast(
        account_id=data.account_id,
        message=data.message,
        recipients=data.recipients,
        media_path=media_path,
        status="pending",
        scheduled_at=scheduled_at,
        recurring_interval_minutes=data.recurring_interval_minutes,
        # If recurring, set first next_scheduled_at to now (will be recalculated
        # after the first send). If immediate, next_scheduled_at = now + interval.
        next_scheduled_at=None,
    )
    if data.recurring_interval_minutes is not None:
        # For recurring broadcasts: if immediate send, set first due immediately
        if scheduled_at is None or scheduled_at <= now:
            broadcast.next_scheduled_at = now
        else:
            broadcast.next_scheduled_at = scheduled_at

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
    """Returns both one-time scheduled broadcasts that are due AND
    recurring parent broadcasts whose next_scheduled_at is due.

    Filters out cancelled or paused recurring broadcasts.
    """
    now = utcnow_naive()

    # One-time scheduled broadcasts that are due
    one_time_query = select(Broadcast).where(
        Broadcast.status == "pending",
        Broadcast.recurring_interval_minutes.is_(None),
        Broadcast.scheduled_at.is_not(None),
        Broadcast.scheduled_at <= now,
    )

    # Recurring parent broadcasts whose next_scheduled_at is due
    recurring_query = select(Broadcast).where(
        Broadcast.recurring_interval_minutes.is_not(None),
        Broadcast.status != "cancelled",
        Broadcast.is_recurring_paused == False,  # noqa: E712
        Broadcast.next_scheduled_at.is_not(None),
        Broadcast.next_scheduled_at <= now,
    )

    one_time_result = await db.execute(one_time_query)
    recurring_result = await db.execute(recurring_query)

    return list(one_time_result.scalars().all()) + list(recurring_result.scalars().all())


async def claim_broadcast_dispatch(db: AsyncSession, broadcast_id: str) -> bool:
    """Atomically claim a broadcast for dispatch.

    Sets status to 'sending' only if currently 'pending'.
    Returns True if claimed, False if another tick/worker already claimed it.
    """
    result = await db.execute(
        select(Broadcast).where(
            Broadcast.id == broadcast_id,
            Broadcast.status == "pending",
        ).with_for_update()
    )
    broadcast = result.scalar_one_or_none()
    if broadcast is None:
        return False
    broadcast.status = "sending"
    await db.commit()
    return True


async def record_broadcast_error(db: AsyncSession, broadcast_id: str, error_message: str) -> None:
    """Record a safe error message on a broadcast without changing its status.

    Only updates if the broadcast is still in a non-terminal state.
    """
    broadcast = await db.get(Broadcast, broadcast_id)
    if broadcast is None:
        return
    if broadcast.status in ("sent", "failed", "cancelled"):
        return
    broadcast.error_message = error_message[:500]  # truncate to fit column
    await db.commit()


async def retry_broadcast(db: AsyncSession, broadcast_id: str) -> Broadcast | None:
    """Reset a failed broadcast to pending for retry.

    Only transitions if current status is ``"failed"``.
    Clears ``status`` → ``"pending"``, ``error_message`` → ``None``,
    ``sent_at`` → ``None``.  Increments ``retry_count``.

    Returns the updated broadcast, or ``None`` if the broadcast is not in a
    retryable state (not found, not ``"failed"``, or retry limit reached).
    """
    broadcast = await db.get(Broadcast, broadcast_id)
    if broadcast is None:
        return None
    if broadcast.status != "failed":
        return None

    max_retries = settings.broadcast_max_retries
    if broadcast.retry_count >= max_retries:
        return None

    broadcast.status = "pending"
    broadcast.error_message = None
    broadcast.sent_at = None
    broadcast.retry_count = broadcast.retry_count + 1
    await db.commit()
    await db.refresh(broadcast)
    return broadcast


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


# ── Recurring broadcast CRUD ──────────────────────────────────────


async def list_recurring_broadcasts(db: AsyncSession) -> list[Broadcast]:
    """Return all active (non-cancelled) recurring broadcasts."""
    result = await db.execute(
        select(Broadcast).where(
            Broadcast.recurring_interval_minutes.is_not(None),
            Broadcast.status != "cancelled",
        ).order_by(Broadcast.created_at.desc())
    )
    return list(result.scalars().all())


async def cancel_recurring_broadcast(db: AsyncSession, broadcast_id: str) -> Broadcast | None:
    """Cancel a recurring broadcast by setting status='cancelled' and cancelled_at.

    Only works on broadcasts that have recurring_interval_minutes set and are
    not already cancelled. Returns the updated broadcast, or None if not found
    or not a recurring broadcast.
    """
    broadcast = await db.get(Broadcast, broadcast_id)
    if broadcast is None:
        return None
    if broadcast.recurring_interval_minutes is None:
        return None
    if broadcast.status == "cancelled":
        return broadcast

    now = utcnow_naive()
    broadcast.status = "cancelled"
    broadcast.cancelled_at = now
    broadcast.next_scheduled_at = None
    await db.commit()
    await db.refresh(broadcast)
    return broadcast


async def reschedule_recurring_broadcast(db: AsyncSession, broadcast_id: str) -> Broadcast | None:
    """Advance next_scheduled_at for a recurring parent broadcast by its interval.

    Called after a recurring child broadcast completes. If the broadcast has been
    cancelled or paused in the meantime, does nothing.
    """
    broadcast = await db.get(Broadcast, broadcast_id)
    if broadcast is None:
        return None
    if broadcast.recurring_interval_minutes is None:
        return None
    if broadcast.status == "cancelled":
        return None
    if broadcast.is_recurring_paused:
        return None

    now = utcnow_naive()
    broadcast.next_scheduled_at = now + timedelta(minutes=broadcast.recurring_interval_minutes)
    await db.commit()
    await db.refresh(broadcast)
    return broadcast


async def create_recurring_child_broadcast(
    db: AsyncSession, parent: Broadcast, scheduled_at: datetime
) -> Broadcast:
    """Create a child broadcast record for a recurring execution.

    This records the execution in the broadcast history while the parent
    tracks the recurring schedule.
    """
    child = Broadcast(
        account_id=parent.account_id,
        message=parent.message,
        recipients=parent.recipients,
        media_path=parent.media_path,
        status="pending",
        scheduled_at=scheduled_at,
        parent_broadcast_id=parent.id,
    )
    db.add(child)
    await db.commit()
    await db.refresh(child)
    return child


async def list_due_recurring_parents(db: AsyncSession) -> list[Broadcast]:
    """Return recurring parent broadcasts whose next_scheduled_at is due.

    Excludes cancelled, paused, or already-sending ones.
    """
    now = utcnow_naive()
    result = await db.execute(
        select(Broadcast).where(
            Broadcast.recurring_interval_minutes.is_not(None),
            Broadcast.status != "cancelled",
            Broadcast.is_recurring_paused == False,  # noqa: E712
            Broadcast.next_scheduled_at.is_not(None),
            Broadcast.next_scheduled_at <= now,
        )
    )
    return list(result.scalars().all())