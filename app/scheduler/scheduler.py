from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.core.logging import get_logger
from app.crud import broadcast as broadcast_crud
from app.crud import reply_macro as macro_crud
from app.database import async_session_maker
from app.services.broadcast_processor import process_broadcast
from app.services.reply_macro_service import execute_reply_macro
from app.services.usdt_watcher import check_usdt_payments

logger = get_logger(__name__)

# Checked frequently (not every 30 min) so a broadcast scheduled for e.g. 14:05 actually
# fires close to 14:05, not up to half an hour late. Also the natural retry interval for
# a scheduled broadcast that's still within another one's 1-per-minute cooldown — see
# below, it's simply left "pending" and picked up again on the next tick.
DISPATCH_INTERVAL_SECONDS = 30

scheduler = AsyncIOScheduler()


async def dispatch_due_broadcasts() -> None:
    async with async_session_maker() as db:
        due = await broadcast_crud.list_due_scheduled_broadcasts(db)
        ready_ids: list[str] = []
        for broadcast in due:
            wait_seconds = await broadcast_crud.seconds_until_next_allowed_broadcast(
                db, broadcast.account_id, exclude_id=broadcast.id
            )
            if wait_seconds > 0:
                logger.info(
                    "scheduled_broadcast_deferred_rate_limited",
                    broadcast_id=broadcast.id,
                    account_id=broadcast.account_id,
                    wait_seconds=round(wait_seconds, 1),
                )
                continue
            ready_ids.append(broadcast.id)

    for broadcast_id in ready_ids:
        logger.info("scheduled_broadcast_dispatched", broadcast_id=broadcast_id)
        await process_broadcast(broadcast_id)


async def dispatch_due_reply_macros() -> None:
    """Dispatch all reply macros that are due to be sent now.
    
    Uses atomic claim_macro_dispatch to prevent double-execution
    across concurrent ticks, multiple workers, or restart.
    """
    async with async_session_maker() as db:
        due_macros = await macro_crud.list_active_macros_due(db)

    for macro in due_macros:
        # Atomically claim this dispatch — skip if another tick/worker won
        async with async_session_maker() as db:
            claimed = await macro_crud.claim_macro_dispatch(db, macro.id)
        if not claimed:
            logger.info("reply_macro_skipped_already_claimed", macro_id=macro.id)
            continue

        logger.info("reply_macro_dispatched", macro_id=macro.id, account_id=macro.account_id)
        await execute_reply_macro(macro.id)


def start_scheduler() -> None:
    scheduler.add_job(
        dispatch_due_broadcasts,
        IntervalTrigger(seconds=DISPATCH_INTERVAL_SECONDS),
        id="dispatch_due_broadcasts",
        replace_existing=True,
    )
    scheduler.add_job(
        dispatch_due_reply_macros,
        IntervalTrigger(seconds=DISPATCH_INTERVAL_SECONDS),
        id="dispatch_due_reply_macros",
        replace_existing=True,
    )
    scheduler.add_job(
        check_usdt_payments,
        IntervalTrigger(minutes=5),
        id="check_usdt_payments",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("scheduler_started", interval_seconds=DISPATCH_INTERVAL_SECONDS)


def shutdown_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("scheduler_stopped")
