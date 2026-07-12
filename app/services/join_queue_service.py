"""Smart Join Queue — sequential, rate-limited group/channel join processor.

Integrates with Bulk Link Inspector: users inspect links, select active ones,
add them to the queue, and the scheduler processes them one at a time per
account at a configurable conservative rate.

Respects Telegram rate limits and FloodWait:
- On FloodWaitError: pauses the item, records flood_wait_until, and skips
  the account until the wait expires.
- On success: records the join in GroupJoinLog (shared audit trail with
  group search and link inspector).
- On failure: marks the item as failed with the error message.

Design patterns inherited from:
- broadcast_processor.py (sequential processing, status machine)
- group_search_service.py (join via JoinChannelRequest/ImportChatInviteRequest)
- link_inspector_service.py (link parsing, entity resolution)
"""

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional

from telethon.errors import FloodWaitError
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest

from app.core.logging import get_logger
from app.crud import join_queue as queue_crud
from app.database import async_session_maker
from app.models.account import Account
from app.models.join_queue import JoinQueueItem
from app.services.link_inspector_service import parse_telegram_link
from app.services.telegram_actions import get_authorized_client

logger = get_logger(__name__)

# Source identifier for join audit logs
JOIN_LOG_SOURCE = "smart_join_queue"


class QueuePausedError(Exception):
    """Raised when the queue is paused for the account."""
    pass


class DailyJoinLimitExceededError(Exception):
    """Raised when the daily join limit is reached."""
    pass


class FloodWaitBackoffError(Exception):
    """Raised when a FloodWait is active for the account."""
    pass


async def process_next_queue_item(account_id: str) -> bool:
    """Process the next queued item for an account.

    Returns True if an item was processed (success, failure, or flood_wait),
    False if no items remain or the queue is paused.

    This is the core tick function called by the scheduler.
    """
    async with async_session_maker() as db:
        # 1. Check queue config
        config = await queue_crud.get_or_create_config(db, account_id)
        if config.is_paused:
            logger.info("queue_paused_skipped", account_id=account_id)
            return False

        # 2. Check daily join limit
        joined_today = await queue_crud.count_today_joins(db, account_id)
        if joined_today >= config.max_daily_joins:
            logger.info(
                "queue_daily_limit_reached",
                account_id=account_id,
                joined_today=joined_today,
                max_daily_joins=config.max_daily_joins,
            )
            return False

        # 3. Check rate limit (joins_per_hour)
        #    Count successful joins in the last hour
        one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        from sqlalchemy import select, func, and_
        from app.models.group_search import GroupJoinLog
        recent_joins_result = await db.execute(
            select(func.count(GroupJoinLog.id)).where(
                and_(
                    GroupJoinLog.account_id == account_id,
                    GroupJoinLog.created_at >= one_hour_ago,
                    GroupJoinLog.success == True,
                )
            )
        )
        recent_joins = recent_joins_result.scalar()
        if recent_joins >= config.joins_per_hour:
            logger.info(
                "queue_rate_limit_reached",
                account_id=account_id,
                recent_joins=recent_joins,
                joins_per_hour=config.joins_per_hour,
            )
            return False

        # 4. Check for active FloodWait on any item
        now = datetime.now(timezone.utc)
        active_flood = await db.execute(
            select(func.count(JoinQueueItem.id)).where(
                and_(
                    JoinQueueItem.account_id == account_id,
                    JoinQueueItem.status == "flood_wait",
                    JoinQueueItem.flood_wait_until > now,
                )
            )
        )
        if active_flood.scalar() > 0:
            logger.info("queue_flood_wait_active", account_id=account_id)
            return False

        # 5. Atomically claim the next queued item
        item = await queue_crud.claim_next_queued(db, account_id)
        if item is None:
            logger.info("queue_empty", account_id=account_id)
            return False

        item_id = item.id
        raw_link = item.raw_link
        title = item.title or raw_link

    # ── Process the item outside the claim transaction ──────────────────
    async with async_session_maker() as db:
        account = await db.get(Account, account_id)
        if account is None:
            await queue_crud.update_queue_item_status(
                db, item, "failed", error_message="계정을 찾을 수 없습니다."
            )
            logger.error("queue_account_not_found", account_id=account_id, item_id=str(item_id))
            return True

    try:
        client = await get_authorized_client(account)
    except Exception as exc:
        async with async_session_maker() as db:
            item = await queue_crud.get_queue_item(db, str(item_id))
            if item:
                await queue_crud.update_queue_item_status(
                    db, item, "failed", error_message=f"계정 인증 실패: {exc}"
                )
        logger.error("queue_auth_failed", account_id=account_id, item_id=str(item_id), error=str(exc))
        return True

    # Execute the join
    success = False
    error_msg = None
    resolved_chat_id = None
    resolved_username = None
    flood_wait_until = None

    try:
        kind, value = parse_telegram_link(raw_link)

        if kind == "username":
            entity = await client.get_entity(value)
            resolved_username = value
            resolved_chat_id = str(entity.id)
            await client(JoinChannelRequest(entity))
        elif kind == "invite":
            updates = await client(ImportChatInviteRequest(value))
            joined_chat = updates.chats[0] if getattr(updates, "chats", None) else None
            resolved_chat_id = str(joined_chat.id) if joined_chat is not None else None
        else:
            raise ValueError("유효하지 않은 링크입니다.")

        success = True
        logger.info("queue_join_success", account_id=account_id, title=title, chat_id=resolved_chat_id)

    except FloodWaitError as exc:
        flood_wait_until = datetime.now(timezone.utc) + timedelta(seconds=exc.seconds)
        error_msg = f"텔레그램 속도 제한: {exc.seconds}초 후 다시 시도해주세요."
        logger.warning(
            "queue_flood_wait",
            account_id=account_id,
            title=title,
            wait_seconds=exc.seconds,
        )
    except Exception as exc:
        error_msg = str(exc)
        logger.warning("queue_join_failed", account_id=account_id, title=title, error=error_msg)

    # ── Persist result ──────────────────────────────────────────────────
    async with async_session_maker() as db:
        item = await queue_crud.get_queue_item(db, str(item_id))
        if item is None:
            logger.warning("queue_item_not_found_for_update", item_id=str(item_id))
            return True

        if flood_wait_until is not None:
            await queue_crud.update_queue_item_status(
                db, item, "flood_wait",
                error_message=error_msg,
                flood_wait_until=flood_wait_until,
            )
        elif success:
            await queue_crud.update_queue_item_status(
                db, item, "success",
                chat_id=resolved_chat_id,
            )
            # Create join audit log (reuses GroupJoinLog)
            await queue_crud.create_join_log(
                db,
                account_id=account_id,
                chat_id=resolved_chat_id or "",
                title=title,
                username=resolved_username,
                keyword=JOIN_LOG_SOURCE,
                success=True,
            )
        else:
            await queue_crud.update_queue_item_status(
                db, item, "failed",
                error_message=error_msg,
            )
            # Also log the failure
            await queue_crud.create_join_log(
                db,
                account_id=account_id,
                chat_id=resolved_chat_id or "",
                title=title,
                username=resolved_username,
                keyword=JOIN_LOG_SOURCE,
                success=False,
                error_message=error_msg,
            )

    return True


async def process_all_accounts() -> None:
    """Process one queued item for every account that has items.

    Called by the scheduler on each tick. Each account gets at most one
    item processed per tick to keep the rate conservative and fair.
    """
    async with async_session_maker() as db:
        # Find all accounts with queued items
        from sqlalchemy import select, func, and_
        from app.models.join_queue import JoinQueueItem

        result = await db.execute(
            select(JoinQueueItem.account_id)
            .where(JoinQueueItem.status == "queued")
            .distinct()
        )
        account_ids = [row[0] for row in result.all()]

    if not account_ids:
        return

    logger.info("queue_tick_started", account_count=len(account_ids))

    for account_id in account_ids:
        try:
            await process_next_queue_item(account_id)
        except Exception as exc:
            logger.error(
                "queue_account_processing_failed",
                account_id=account_id,
                error=str(exc),
            )

    logger.info("queue_tick_completed", account_count=len(account_ids))


async def recover_stale_flood_wait_items() -> None:
    """Recover items stuck in flood_wait whose wait has expired.

    Resets them to 'queued' so the next tick re-processes them.
    Called at the start of each scheduler tick.
    """
    from sqlalchemy import update as sa_update

    async with async_session_maker() as db:
        now = datetime.now(timezone.utc)
        result = await db.execute(
            select(JoinQueueItem).where(
                and_(
                    JoinQueueItem.status == "flood_wait",
                    JoinQueueItem.flood_wait_until <= now,
                )
            )
        )
        stale_items = list(result.scalars().all())
        for item in stale_items:
            item.status = "queued"
            item.flood_wait_until = None
            logger.info(
                "queue_flood_wait_recovered",
                item_id=str(item.id),
                account_id=item.account_id,
            )
        await db.commit()
        return len(stale_items)