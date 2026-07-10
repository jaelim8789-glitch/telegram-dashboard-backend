"""Scheduler — recurring dispatch of due broadcasts and reply macros.

Reliability semantics (Sprint 22):
- Each dispatch function wraps execution in try/except to prevent one
  failure from crashing the entire tick.
- Broadcasts use atomic claim (status='sending') to prevent duplicate
  concurrent execution across ticks or workers.
- Reply macros already use claim_macro_dispatch for atomicity.
- Failed executions record safe error messages on the schedule record
  without disabling valid schedules.
- In-memory concurrency guard prevents the same schedule from being
  dispatched twice simultaneously within the same process.
"""

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.core.logging import get_logger
from app.crud import broadcast as broadcast_crud
from app.crud import reply_macro as macro_crud
from app.database import async_session_maker
from app.services.broadcast_processor import process_broadcast, process_recurring_parent
from app.services.reply_macro_service import execute_reply_macro
from app.services.usdt_watcher import check_usdt_payments

logger = get_logger(__name__)

DISPATCH_INTERVAL_SECONDS = 30

scheduler = AsyncIOScheduler()

# In-memory concurrency guard: set of schedule IDs currently being executed.
# Prevents duplicate concurrent execution of the same schedule within this process.
_running_broadcasts: set[str] = set()
_running_recurring: set[str] = set()
_running_macros: set[str] = set()


async def dispatch_due_broadcasts() -> None:
    """Dispatch all due scheduled broadcasts with error isolation.

    Each broadcast is processed independently — one failure does not
    block others. Atomic claim via status='sending' prevents duplicate
    execution across ticks or workers.

    Recurring parent broadcasts are dispatched via process_recurring_parent
    which creates a child record first for history tracking.

    Stale recurring parents (crashed workers) are recovered at the start of
    each tick.  Recovery resets them to "pending" so the rest of this tick
    re-dispatches them.
    """
    # Recover stale recurring parents first so they are eligible this tick
    try:
        async with async_session_maker() as db:
            recovered = await broadcast_crud.recover_stale_recurring_parents(db)
            if recovered:
                logger.info("recurring_stale_recovered", count=len(recovered))
    except Exception as exc:
        logger.error("recurring_stale_recovery_failed", error=str(exc))

    async with async_session_maker() as db:
        due = await broadcast_crud.list_due_scheduled_broadcasts(db)
        one_time_ids: list[str] = []
        recurring_ids: list[str] = []
        for broadcast in due:
            # Skip if already running in this process
            if broadcast.recurring_interval_minutes is not None:
                # Recurring parent
                if broadcast.id in _running_recurring:
                    logger.info("recurring_skipped_already_running", parent_id=broadcast.id)
                    continue
                recurring_ids.append(broadcast.id)
            else:
                # One-time broadcast
                if broadcast.id in _running_broadcasts:
                    logger.info("scheduled_broadcast_skipped_already_running", broadcast_id=broadcast.id)
                    continue

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
                one_time_ids.append(broadcast.id)

    # Dispatch one-time broadcasts (existing flow)
    for broadcast_id in one_time_ids:
        async with async_session_maker() as db:
            claimed = await broadcast_crud.claim_broadcast_dispatch(db, broadcast_id)
        if not claimed:
            logger.info("scheduled_broadcast_skipped_already_claimed", broadcast_id=broadcast_id)
            continue

        _running_broadcasts.add(broadcast_id)
        try:
            logger.info("scheduled_broadcast_dispatched", broadcast_id=broadcast_id)
            await process_broadcast(broadcast_id)
        except Exception as exc:
            logger.error(
                "scheduled_broadcast_failed",
                broadcast_id=broadcast_id,
                error=str(exc),
            )
            # Record the error on the broadcast so it's visible to the user
            try:
                async with async_session_maker() as db:
                    await broadcast_crud.record_broadcast_error(db, broadcast_id, str(exc))
            except Exception as persist_err:
                logger.error(
                    "scheduled_broadcast_error_persist_failed",
                    broadcast_id=broadcast_id,
                    error=str(persist_err),
                )
        finally:
            _running_broadcasts.discard(broadcast_id)

    # Dispatch recurring parent broadcasts
    for parent_id in recurring_ids:
        # Atomic claim: set status to 'sending' on the parent
        async with async_session_maker() as db:
            claimed = await broadcast_crud.claim_broadcast_dispatch(db, parent_id)
        if not claimed:
            logger.info("recurring_skipped_already_claimed", parent_id=parent_id)
            continue

        _running_recurring.add(parent_id)
        try:
            logger.info("recurring_dispatched", parent_id=parent_id)
            await process_recurring_parent(parent_id)
        except Exception as exc:
            logger.error(
                "recurring_failed",
                parent_id=parent_id,
                error=str(exc),
            )
        finally:
            _running_recurring.discard(parent_id)


async def dispatch_due_reply_macros() -> None:
    """Dispatch all reply macros that are due to be sent now.

    Uses atomic claim_macro_dispatch to prevent double-execution
    across concurrent ticks, multiple workers, or restart.
    Each macro is processed independently — one failure does not
    block others.
    """
    async with async_session_maker() as db:
        due_macros = await macro_crud.list_active_macros_due(db)

    for macro in due_macros:
        # Skip if already running in this process
        if macro.id in _running_macros:
            logger.info("reply_macro_skipped_already_running", macro_id=macro.id)
            continue

        # Atomically claim this dispatch — skip if another tick/worker won
        async with async_session_maker() as db:
            claimed = await macro_crud.claim_macro_dispatch(db, macro.id)
        if not claimed:
            logger.info("reply_macro_skipped_already_claimed", macro_id=macro.id)
            continue

        _running_macros.add(macro.id)
        try:
            logger.info("reply_macro_dispatched", macro_id=macro.id, account_id=macro.account_id)
            await execute_reply_macro(macro.id)
        except Exception as exc:
            logger.error(
                "reply_macro_failed",
                macro_id=macro.id,
                error=str(exc),
            )
        finally:
            _running_macros.discard(macro.id)


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