from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.core.logging import get_logger
from app.crud import broadcast as broadcast_crud
from app.database import async_session_maker
from app.services.broadcast_processor import process_broadcast

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


def start_scheduler() -> None:
    scheduler.add_job(
        dispatch_due_broadcasts,
        IntervalTrigger(seconds=DISPATCH_INTERVAL_SECONDS),
        id="dispatch_due_broadcasts",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("scheduler_started", interval_seconds=DISPATCH_INTERVAL_SECONDS)


def shutdown_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("scheduler_stopped")
