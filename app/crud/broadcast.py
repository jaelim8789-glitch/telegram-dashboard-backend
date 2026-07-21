from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Identity
from app.config import settings
from app.core.limits import BROADCAST_MIN_INTERVAL_SECONDS, effective_broadcast_interval
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
        delay_seconds=getattr(data, "delay_seconds", None),
        inline_buttons=getattr(data, "inline_buttons", None),
        group_ids=getattr(data, "group_ids", None),
        campaign_id=getattr(data, "campaign_id", None),
        batch_size=getattr(data, "batch_size", None),
        content_studio_content_id=getattr(data, "content_studio_content_id", None),
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
    db: AsyncSession, account_id: str, *, exclude_id: str | None = None, batch_size: int | None = None
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
    interval = effective_broadcast_interval(batch_size)
    elapsed = (now - last_time).total_seconds()
    return max(0.0, interval - elapsed)


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

    max_retries = settings.broadcast_max_retries
    result = await db.execute(
        update(Broadcast)
        .where(
            Broadcast.id == broadcast_id,
            Broadcast.status == "failed",
            Broadcast.retry_count < max_retries,
        )
        .values(
            status="retrying",
            error_message=None,
            sent_at=None,
            retry_count=Broadcast.retry_count + 1,
        )
    )
    if result.rowcount == 0:
        return None

    await db.commit()
    await db.refresh(broadcast)
    return broadcast


async def batch_retry_broadcasts(db: AsyncSession, broadcast_ids: list[str], identity: Identity) -> list[dict]:
    """Batch retry multiple failed broadcasts at once.

    Returns a list of {id, status, error} dicts for each attempted retry.
    Broadcasts not belonging to the caller's tenant are silently skipped.
    """
    results: list[dict] = []
    for bid in broadcast_ids:
        broadcast = await db.get(Broadcast, bid)
        if broadcast is None:
            results.append({"id": bid, "status": "skipped", "error": "발송 작업을 찾을 수 없습니다."})
            continue

        # Tenant check
        account = await db.get(Account, broadcast.account_id)
        if account is None:
            results.append({"id": bid, "status": "skipped", "error": "계정을 찾을 수 없습니다."})
            continue
        if identity.kind != "admin" and identity.tenant_id and account.tenant_id != identity.tenant_id:
            results.append({"id": bid, "status": "skipped", "error": "접근 권한이 없습니다."})
            continue

        if broadcast.status != "failed":
            results.append({"id": bid, "status": "skipped", "error": f"현재 상태({broadcast.status})에서 재시도할 수 없습니다."})
            continue

        if broadcast.retry_count >= settings.broadcast_max_retries:
            results.append({"id": bid, "status": "skipped", "error": f"최대 재시도 횟수({settings.broadcast_max_retries}회) 초과"})
            continue

        updated = await retry_broadcast(db, bid)
        if updated is None:
            results.append({"id": bid, "status": "failed", "error": "재시도 처리 중 상태가 변경되었습니다."})
        else:
            results.append({"id": bid, "status": "retried"})

    return results


async def list_upcoming_scheduled_broadcasts(db: AsyncSession, identity: Identity | None = None) -> list[Broadcast]:
    now = utcnow_naive()
    query = select(Broadcast).where(
        or_(
            (Broadcast.status == "pending")
            & Broadcast.scheduled_at.is_not(None)
            & (Broadcast.scheduled_at > now),
            (Broadcast.recurring_interval_minutes.is_not(None))
            & Broadcast.next_scheduled_at.is_not(None)
            & (Broadcast.next_scheduled_at > now)
            & (Broadcast.status != "cancelled")
            & (Broadcast.is_recurring_paused.is_(False)),
        )
    )

    if identity is not None and identity.kind != "admin":
        if identity.tenant_id:
            account_ids = select(Account.id).where(Account.tenant_id == identity.tenant_id)
            query = query.where(Broadcast.account_id.in_(account_ids))
        else:
            return []

    query = query.order_by(func.coalesce(Broadcast.next_scheduled_at, Broadcast.scheduled_at).asc())
    result = await db.execute(query)
    return list(result.scalars().all())


async def summarize_message_log_outcomes(
    db: AsyncSession, broadcast_id: str, total_recipients: int
) -> tuple[bool, bool, int]:
    from app.models.message_log import MessageLog

    result = await db.execute(
        select(func.count(func.distinct(MessageLog.recipient))).where(
            MessageLog.source == "broadcast",
            MessageLog.source_id == broadcast_id,
            MessageLog.success.is_(True),
        )
    )
    succeeded_count = result.scalar_one() or 0
    any_success = succeeded_count > 0
    all_success = total_recipients > 0 and succeeded_count >= total_recipients
    return any_success, all_success, succeeded_count


async def get_succeeded_recipients(db: AsyncSession, broadcast_id: str) -> set[str]:
    from app.models.message_log import MessageLog

    result = await db.execute(
        select(MessageLog.recipient).where(
            MessageLog.source == "broadcast",
            MessageLog.source_id == broadcast_id,
            MessageLog.success.is_(True),
        ).distinct()
    )
    return set(result.scalars().all())


async def list_logs(
    db: AsyncSession,
    *,
    identity: Identity | None = None,
    account_id: str | None = None,
    status: str | None = None,
    date: str | None = None,
    page: int = 1,
    limit: int = 50,
) -> list[Broadcast]:
    offset = (page - 1) * limit
    query = select(Broadcast)

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
    query = query.offset(offset).limit(limit)
    result = await db.execute(query)
    return list(result.scalars().all())


# ── Recurring broadcast CRUD ──────────────────────────────────────


RECURRING_STALE_TIMEOUT_SECONDS = 120


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
        delivery_mode=parent.delivery_mode,
        reply_to_msg_id=parent.reply_to_msg_id,
        campaign_id=parent.campaign_id,
        delay_seconds=parent.delay_seconds,
        inline_buttons=parent.inline_buttons,
        group_ids=parent.group_ids,
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


async def list_distribution_siblings(db: AsyncSession, batch_id: str) -> list[Broadcast]:
    """All broadcasts created from one multi-account distribution request
    (see app/services/broadcast_distribution.py), across every account it
    was split to."""
    result = await db.execute(
        select(Broadcast)
        .where(Broadcast.distribution_batch_id == batch_id)
        .order_by(Broadcast.created_at.asc())
    )
    return list(result.scalars().all())


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


# ── Send-to-Group CRUD ─────────────────────────────────────────────


async def mark_groups_resolved(db: AsyncSession, broadcast_id: str) -> Broadcast | None:
    broadcast = await db.get(Broadcast, broadcast_id)
    if broadcast is None:
        return None
    broadcast.groups_resolved = True
    await db.commit()
    await db.refresh(broadcast)
    return broadcast


async def list_failed_broadcasts_for_account(db: AsyncSession, account_id: str) -> list[Broadcast]:
    result = await db.execute(
        select(Broadcast)
        .where(
            Broadcast.account_id == account_id,
            Broadcast.status == "failed",
        )
        .order_by(Broadcast.created_at.desc())
    )
    return list(result.scalars().all())


async def search_broadcasts(
    db: AsyncSession,
    *,
    identity: Identity | None = None,
    account_id: str | None = None,
    status: str | None = None,
    message_search: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    campaign_id: str | None = None,
    sort_by: str = "created_at",
    sort_order: str = "desc",
    skip: int = 0,
    limit: int = 50,
) -> tuple[list[Broadcast], int]:
    from sqlalchemy import func as sa_func, or_
    from app.models.account import Account

    conditions = []

    if identity is not None and identity.kind != "admin" and account_id is None:
        if identity.tenant_id:
            account_ids_subq = select(Account.id).where(Account.tenant_id == identity.tenant_id)
            conditions.append(Broadcast.account_id.in_(account_ids_subq))
        else:
            return [], 0

    if account_id:
        conditions.append(Broadcast.account_id == account_id)
    if status:
        if "," in status:
            conditions.append(Broadcast.status.in_([s.strip() for s in status.split(",")]))
        else:
            conditions.append(Broadcast.status == status)
    if message_search:
        conditions.append(Broadcast.message.ilike(f"%{message_search}%"))
    if date_from:
        try:
            from datetime import datetime as dt
            d = dt.strptime(date_from, "%Y-%m-%d")
            conditions.append(Broadcast.created_at >= d)
        except ValueError:
            pass
    if date_to:
        try:
            from datetime import datetime as dt, timedelta
            d = dt.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
            conditions.append(Broadcast.created_at < d)
        except ValueError:
            pass
    if campaign_id:
        conditions.append(Broadcast.campaign_id == campaign_id)

    count_q = select(sa_func.count()).select_from(Broadcast).where(*conditions)
    total = await db.scalar(count_q) or 0

    order_col = getattr(Broadcast, sort_by, Broadcast.created_at)
    order_fn = order_col.asc() if sort_order == "asc" else order_col.desc()

    q = (
        select(Broadcast)
        .where(*conditions)
        .order_by(order_fn)
        .offset(skip)
        .limit(limit)
    )
    result = await db.execute(q)
    return list(result.scalars().all()), total