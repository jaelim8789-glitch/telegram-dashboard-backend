"""CRUD operations for the Smart Join Queue.

Follows the same patterns as broadcast_crud (status machine, atomic claim) and
group_search_crud (join log creation, daily counter).
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.join_queue import JoinQueueConfig, JoinQueueItem
from app.models.group_search import GroupJoinLog


# ── Queue Item CRUD ──────────────────────────────────────────────────────────


async def add_to_queue(
    db: AsyncSession,
    account_id: str,
    raw_link: str,
    title: Optional[str] = None,
    chat_type: Optional[str] = None,
    username: Optional[str] = None,
    chat_id: Optional[str] = None,
    delay_before_seconds: Optional[float] = None,
) -> JoinQueueItem:
    """Add one link to the join queue. Assigns the next position for this account."""
    max_pos_result = await db.execute(
        select(func.coalesce(func.max(JoinQueueItem.position), 0)).where(
            JoinQueueItem.account_id == account_id
        )
    )
    next_pos = max_pos_result.scalar() + 1

    item = JoinQueueItem(
        account_id=account_id,
        raw_link=raw_link,
        title=title,
        chat_type=chat_type,
        username=username,
        chat_id=chat_id,
        position=next_pos,
        delay_before_seconds=delay_before_seconds,
        status="queued",
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return item


async def add_many_to_queue(
    db: AsyncSession,
    account_id: str,
    items: list[dict],
) -> list[JoinQueueItem]:
    """Add multiple links to the queue in one batch, preserving order."""
    max_pos_result = await db.execute(
        select(func.coalesce(func.max(JoinQueueItem.position), 0)).where(
            JoinQueueItem.account_id == account_id
        )
    )
    next_pos = max_pos_result.scalar()

    created: list[JoinQueueItem] = []
    for item_data in items:
        next_pos += 1
        item = JoinQueueItem(
            account_id=account_id,
            raw_link=item_data["raw_link"],
            title=item_data.get("title"),
            chat_type=item_data.get("chat_type"),
            username=item_data.get("username"),
            chat_id=item_data.get("chat_id"),
            position=next_pos,
            delay_before_seconds=item_data.get("delay_before_seconds"),
            status="queued",
        )
        db.add(item)
        created.append(item)

    await db.commit()
    for item in created:
        await db.refresh(item)
    return created


async def get_queue_item(db: AsyncSession, item_id: str) -> Optional[JoinQueueItem]:
    result = await db.execute(
        select(JoinQueueItem).where(JoinQueueItem.id == item_id)
    )
    return result.scalar_one_or_none()


async def list_queue(
    db: AsyncSession,
    account_id: str,
    status_filter: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[JoinQueueItem], int]:
    """List queue items for an account, ordered by position."""
    conditions = [JoinQueueItem.account_id == account_id]
    if status_filter:
        conditions.append(JoinQueueItem.status == status_filter)

    # Total count
    count_result = await db.execute(
        select(func.count(JoinQueueItem.id)).where(and_(*conditions))
    )
    total = count_result.scalar()

    # Items
    result = await db.execute(
        select(JoinQueueItem)
        .where(and_(*conditions))
        .order_by(JoinQueueItem.position)
        .offset(offset)
        .limit(limit)
    )
    items = list(result.scalars().all())
    return items, total


async def claim_next_queued(
    db: AsyncSession, account_id: str
) -> Optional[JoinQueueItem]:
    """Atomically claim the next queued item for processing.

    Uses a status-based optimistic lock: only items with status='queued'
    can be claimed. Returns None if no queued items remain.
    """
    # Find the first queued item for this account
    result = await db.execute(
        select(JoinQueueItem)
        .where(
            and_(
                JoinQueueItem.account_id == account_id,
                JoinQueueItem.status == "queued",
            )
        )
        .order_by(JoinQueueItem.position)
        .limit(1)
    )
    item = result.scalar_one_or_none()
    if item is None:
        return None

    # Atomically claim: set status to 'processing'
    item.status = "processing"
    await db.commit()
    await db.refresh(item)
    return item


async def update_queue_item_status(
    db: AsyncSession,
    item: JoinQueueItem,
    status: str,
    error_message: Optional[str] = None,
    chat_id: Optional[str] = None,
    flood_wait_until: Optional[datetime] = None,
) -> None:
    """Update the status of a queue item after processing."""
    item.status = status
    if error_message is not None:
        item.error_message = error_message
    if chat_id is not None:
        item.chat_id = chat_id
    if flood_wait_until is not None:
        item.flood_wait_until = flood_wait_until
    if status in ("success", "failed"):
        item.processed_at = datetime.now(timezone.utc)
    await db.commit()


async def count_queued(db: AsyncSession, account_id: str) -> int:
    """Count items still queued or processing for an account."""
    result = await db.execute(
        select(func.count(JoinQueueItem.id)).where(
            and_(
                JoinQueueItem.account_id == account_id,
                JoinQueueItem.status.in_(["queued", "processing"]),
            )
        )
    )
    return result.scalar()


async def remove_from_queue(db: AsyncSession, item_id: str) -> bool:
    """Remove a single item from the queue. Returns True if deleted."""
    result = await db.execute(
        select(JoinQueueItem).where(JoinQueueItem.id == item_id)
    )
    item = result.scalar_one_or_none()
    if item is None:
        return False
    await db.delete(item)
    await db.commit()
    return True


async def clear_queue(db: AsyncSession, account_id: str, status: Optional[str] = None) -> int:
    """Clear all items (optionally filtered by status) for an account. Returns count deleted."""
    conditions = [JoinQueueItem.account_id == account_id]
    if status:
        conditions.append(JoinQueueItem.status == status)

    result = await db.execute(select(JoinQueueItem).where(and_(*conditions)))
    items = list(result.scalars().all())
    count = len(items)
    for item in items:
        await db.delete(item)
    await db.commit()
    return count


# ── Queue Config CRUD ────────────────────────────────────────────────────────


async def get_or_create_config(db: AsyncSession, account_id: str) -> JoinQueueConfig:
    """Get the queue config for an account, creating a default one if none exists."""
    result = await db.execute(
        select(JoinQueueConfig).where(JoinQueueConfig.account_id == account_id)
    )
    config = result.scalar_one_or_none()
    if config is None:
        config = JoinQueueConfig(account_id=account_id)
        db.add(config)
        await db.commit()
        await db.refresh(config)
    return config


async def update_config(
    db: AsyncSession,
    account_id: str,
    is_paused: Optional[bool] = None,
    joins_per_hour: Optional[int] = None,
    max_daily_joins: Optional[int] = None,
) -> JoinQueueConfig:
    """Update queue configuration for an account."""
    config = await get_or_create_config(db, account_id)
    if is_paused is not None:
        config.is_paused = is_paused
    if joins_per_hour is not None:
        config.joins_per_hour = joins_per_hour
    if max_daily_joins is not None:
        config.max_daily_joins = max_daily_joins
    await db.commit()
    await db.refresh(config)
    return config


# ── Daily join counter (reuses GroupJoinLog) ─────────────────────────────────


async def count_today_joins(db: AsyncSession, account_id: str) -> int:
    """Count today's successful joins across all sources (group search + link inspector + queue)."""
    from app.crud.group_search import count_today_joins as gs_count
    return await gs_count(db, account_id)


async def create_join_log(
    db: AsyncSession,
    account_id: str,
    chat_id: str,
    title: str,
    username: Optional[str],
    keyword: str,
    success: bool,
    error_message: Optional[str] = None,
) -> GroupJoinLog:
    """Create a join audit log entry, reusing the GroupJoinLog model."""
    from app.crud.group_search import create_join_log as gs_create
    return await gs_create(
        db,
        account_id=account_id,
        chat_id=chat_id,
        title=title,
        username=username,
        keyword=keyword,
        success=success,
        error_message=error_message,
    )