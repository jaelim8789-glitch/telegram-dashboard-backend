from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Identity
from app.config import settings
from app.core.limits import BROADCAST_MIN_INTERVAL_SECONDS
from app.core.logging import get_logger
from app.models.broadcast import Broadcast
from app.models.account import Account
from app.schemas.broadcast import BroadcastCreate, RECURRING_INTERVAL_VALUES


logger = get_logger(__name__)


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
        next_scheduled_at=None,
        delivery_mode=getattr(data, "delivery_mode", "normal"),
        reply_to_msg_id=getattr(data, "reply_to_msg_id", None),
    )
    if data.recurring_interval_minutes is not None:
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
    now = utcnow_naive()
    reference_time = func.coalesce(Broadcast.sent_at, Broadcast.created_at)
    query = (
        select(reference_time)
        .where(
            Broadcast.account_id == account_id,
            Broadcast.recurring_interval_minutes.is_(None),
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

    one_time_query = select(Broadcast).where(
        Broadcast.status == "pending",
        Broadcast.recurring_interval_minutes.is_(None),
        Broadcast.scheduled_at.is_not(None),
        Broadcast.scheduled_at <= now,
    )

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
    # Stamp sent_at at claim time so recover_stale_recurring_parents has a
    # timestamp to measure staleness against. For recurring parents this is
    # otherwise never set (process_broadcast only touches the child record).
    broadcast.sent_at = utcnow_naive()
    await db.commit()
    return True


async def record_broadcast_error(db: AsyncSession, broadcast_id: str, error_message: str) -> None:
    broadcast = await db.get(Broadcast, broadcast_id)
    if broadcast is None:
        return
    if broadcast.status in ("sent", "failed", "cancelled"):
        return
    broadcast.error_message = error_message[:500]
    await db.commit()


async def retry_broadcast(db: AsyncSession, broadcast_id: str) -> Broadcast | None:
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


async def list_upcoming_scheduled_broadcasts(db: AsyncSession, identity: Identity | None = None) -> list[Broadcast]:
    now = utcnow_naive()
    query = select(Broadcast).where(
        Broadcast.status == "pending",
        Broadcast.scheduled_at.is_not(None),
        Broadcast.scheduled_at > now,
    )

    if identity is not None and identity.kind != "admin":
        if identity.tenant_id:
            account_ids = select(Account.id).where(Account.tenant_id == identity.tenant_id)
            query = query.where(Broadcast.account_id.in_(account_ids))
        else:
            return []

    query = query.order_by(Broadcast.scheduled_at.asc())
    result = await db.execute(query)
    return list(result.scalars().all())


async def list_logs(
    db: AsyncSession,
    *,
    identity: Identity | None = None,
    account_id: str | None = None,
    status: str | None = None,
    date: str | None = None,
) -> list[Broadcast]:
    query = select(Broadcast).where(
        Broadcast.recurring_interval_minutes.is_(None)
    )

    if identity is not None and identity.kind != "admin" and account_id is None:
        if identity.tenant_id:
            account_ids_subq = select(Account.id).where(Account.tenant_id == identity.tenant_id)
            query = query.where(Broadcast.account_id.in_(account_ids_subq))
        else:
            return []

    query = query.order_by(Broadcast.created_at.desc())
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


# How long a recurring parent may stay in "sending" before we consider
# it stale (crashed worker) and recover it.  Must be > DISPATCH_INTERVAL_SECONDS
# * 2 to prevent false-positive recovery while a slow tick is still running.
RECURRING_STALE_TIMEOUT_SECONDS = 120  # 4x the 30s tick interval


async def list_recurring_broadcasts(db: AsyncSession, identity: Identity | None = None) -> list[Broadcast]:
    query = select(Broadcast).where(
        Broadcast.recurring_interval_minutes.is_not(None),
        Broadcast.status != "cancelled",
    )

    if identity is not None and identity.kind != "admin":
        if identity.tenant_id:
            account_ids = select(Account.id).where(Account.tenant_id == identity.tenant_id)
            query = query.where(Broadcast.account_id.in_(account_ids))
        else:
            return []

    query = query.order_by(Broadcast.created_at.desc())
    result = await db.execute(query)
    return list(result.scalars().all())


async def recover_stale_recurring_parents(db: AsyncSession) -> list[Broadcast]:
    """Find and recover recurring parent broadcasts stuck in "sending" beyond
    ``RECURRING_STALE_TIMEOUT_SECONDS`` (120s, configurable at module level).

    Crash windows handled:
      1. Crash immediately after parent claim (status → 'sending', ``sent_at`` set,
         ``next_scheduled_at`` unchanged).  Parent reset to "pending", next tick
         re-dispatches if ``next_scheduled_at`` is past due.
      2. Crash after child creation but before next_scheduled_at advancement
         (status → 'sending', child exists with status 'pending').  Orphan child
         is marked "failed" with safe error message; parent is reset to "pending".
      3. Crash during child dispatch (``next_scheduled_at`` already advanced by
         Bug-1 fix, child mid-flight).  Orphan child (if any) cleaned; parent reset
         so tick re-dispatches if ``next_scheduled_at`` is due.
      4. Crash after child completes, before parent status cleanup.  Same as above.
      5. Restart while recovered work is overdue — only ONE catch-up fires because
         ``next_scheduled_at`` prevents backlog.

    Recovery is safe because:
      - Only recurring parents (``recurring_interval_minutes IS NOT NULL``) are
        touched — never normal broadcasts or child records.
      - Only status='sending' with ``sent_at`` beyond the timeout qualifies.
      - ``with_for_update(skip_locked=True)`` prevents duplicate recovery across
        workers.
      - Cancelled/paused broadcasts are never touched.
      - Orphaned child broadcasts (created before crash, never dispatched) are
        marked "failed" so they don't duplicate history.
      - ``next_scheduled_at`` is left as-is: if it's still in the past, the
        scheduler tick will dispatch; if advanced past now, the tick skips.
    """
    now = utcnow_naive()
    cutoff = now - timedelta(seconds=RECURRING_STALE_TIMEOUT_SECONDS)

    result = await db.execute(
        select(Broadcast).where(
            Broadcast.recurring_interval_minutes.is_not(None),
            Broadcast.status == "sending",
            Broadcast.sent_at.isnot(None),
            Broadcast.sent_at <= cutoff,
        ).with_for_update()
    )
    stale = list(result.scalars().all())

    if not stale:
        return []

    recovered: list[Broadcast] = []
    for parent in stale:
        if parent.status == "cancelled" or parent.is_recurring_paused:
            continue

        # Look for orphaned child created before the crash
        orphan_result = await db.execute(
            select(Broadcast).where(
                Broadcast.parent_broadcast_id == parent.id,
                Broadcast.status == "pending",
            ).with_for_update(skip_locked=True).limit(1)
        )
        orphan = orphan_result.scalar_one_or_none()

        if orphan is not None:
            orphan.status = "failed"
            orphan.error_message = "반복 발송 복구: 중복 방지를 위해 이전 발송을 취소했습니다."
            logger.info(
                "recurring_orphan_cleaned",
                orphan_id=orphan.id,
                parent_id=parent.id,
            )

        parent.status = "pending"
        parent.error_message = None
        recovered.append(parent)

        logger.info(
            "recurring_parent_recovered",
            parent_id=parent.id,
            orphan_cleaned=orphan is not None,
        )

    await db.commit()
    for parent in recovered:
        await db.refresh(parent)
    return recovered


async def cancel_recurring_broadcast(db: AsyncSession, broadcast_id: str) -> Broadcast | None:
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
    # Release the dispatch claim (status set to "sending" by claim_broadcast_dispatch)
    # now that the child has been created and the next occurrence is scheduled.
    # Without this, the parent stays "sending" forever and claim_broadcast_dispatch's
    # `WHERE status == "pending"` check permanently blocks all future occurrences —
    # the recurrence fires exactly once and then silently stops.
    broadcast.status = "pending"
    await db.commit()
    await db.refresh(broadcast)
    return broadcast


async def create_recurring_child_broadcast(
    db: AsyncSession, parent: Broadcast, scheduled_at: datetime
) -> Broadcast:
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


# ── Pause / Unpause ────────────────────────────────────────────────


async def pause_recurring_broadcast(db: AsyncSession, broadcast_id: str) -> Broadcast | None:
    broadcast = await db.get(Broadcast, broadcast_id)
    if broadcast is None:
        return None
    if broadcast.recurring_interval_minutes is None:
        return None
    if broadcast.status == "cancelled":
        return None
    broadcast.is_recurring_paused = True
    await db.commit()
    await db.refresh(broadcast)
    return broadcast


async def unpause_recurring_broadcast(db: AsyncSession, broadcast_id: str) -> Broadcast | None:
    broadcast = await db.get(Broadcast, broadcast_id)
    if broadcast is None:
        return None
    if broadcast.recurring_interval_minutes is None:
        return None
    if broadcast.status == "cancelled":
        return None
    if not broadcast.is_recurring_paused:
        return broadcast
    broadcast.is_recurring_paused = False
    now = utcnow_naive()
    if broadcast.next_scheduled_at is None or broadcast.next_scheduled_at <= now:
        broadcast.next_scheduled_at = now
    await db.commit()
    await db.refresh(broadcast)
    return broadcast


# ── Child broadcast queries (execution history) ────────────────────


async def list_child_broadcasts(
    db: AsyncSession, parent_id: str, limit: int = 20, offset: int = 0
) -> list[Broadcast]:
    result = await db.execute(
        select(Broadcast)
        .where(Broadcast.parent_broadcast_id == parent_id)
        .order_by(Broadcast.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars().all())


async def count_child_broadcasts(db: AsyncSession, parent_id: str) -> int:
    from sqlalchemy import func as sa_func
    result = await db.execute(
        select(sa_func.count(Broadcast.id)).where(Broadcast.parent_broadcast_id == parent_id)
    )
    return result.scalar() or 0


async def get_last_child_broadcast(db: AsyncSession, parent_id: str) -> Broadcast | None:
    result = await db.execute(
        select(Broadcast)
        .where(Broadcast.parent_broadcast_id == parent_id)
        .order_by(Broadcast.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def list_recurring_with_child_counts(
    db: AsyncSession, identity: Identity | None = None,
) -> list[tuple[Broadcast, int, Broadcast | None]]:
    query = select(Broadcast).where(
        Broadcast.recurring_interval_minutes.is_not(None),
        Broadcast.status != "cancelled",
    )

    if identity is not None and identity.kind != "admin":
        if identity.tenant_id:
            account_ids = select(Account.id).where(Account.tenant_id == identity.tenant_id)
            query = query.where(Broadcast.account_id.in_(account_ids))
        else:
            return []

    query = query.order_by(Broadcast.created_at.desc())
    result = await db.execute(query)
    parents = list(result.scalars().all())

    if not parents:
        return []

    parent_ids = [p.id for p in parents]

    from sqlalchemy import func as sa_func

    count_rows = await db.execute(
        select(
            Broadcast.parent_broadcast_id,
            sa_func.count(Broadcast.id).label("cnt"),
        ).where(
            Broadcast.parent_broadcast_id.in_(parent_ids),
        ).group_by(Broadcast.parent_broadcast_id)
    )
    count_map = {row.parent_broadcast_id: row.cnt for row in count_rows.all()}

    all_children = await db.execute(
        select(Broadcast).where(
            Broadcast.parent_broadcast_id.in_(parent_ids),
        ).order_by(Broadcast.parent_broadcast_id, Broadcast.created_at.desc())
    )
    last_map: dict[str, Broadcast] = {}
    for child in all_children.scalars().all():
        if child.parent_broadcast_id not in last_map:
            last_map[child.parent_broadcast_id] = child

    enriched = []
    for parent in parents:
        child_count = count_map.get(parent.id, 0)
        last_child = last_map.get(parent.id)
        enriched.append((parent, child_count, last_child))

    return enriched
