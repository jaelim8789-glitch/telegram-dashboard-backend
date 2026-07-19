"""Scheduler — recurring dispatch of due broadcasts and reply macros.

Reliability semantics (Sprint 22):
- Each dispatch function wraps execution in try/except to prevent one
  failure from crashing the entire tick.
- Broadcasts use atomic claim (status='sending') to prevent duplicate
  execution across ticks or workers.
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
from app.services.ai_ops_service import generate_and_store_ops_report
from app.services.billing import downgrade_expired_tenants, notify_expiring_trials
from app.services.broadcast_processor import process_broadcast, process_recurring_parent
from app.services.join_queue_service import process_all_accounts, recover_stale_flood_wait_items
from app.services.random_reply_service import execute_random_reply
from app.services.usdt_watcher import check_usdt_payments

logger = get_logger(__name__)

DISPATCH_INTERVAL_SECONDS = 30
RANDOM_REPLY_INTERVAL_MINUTES = 30

scheduler = AsyncIOScheduler()

# In-memory concurrency guard: set of schedule IDs currently being executed.
# Prevents duplicate concurrent execution of the same schedule within this process.
_running_broadcasts: set[str] = set()
_running_recurring: set[str] = set()
_running_random_reply: set[str] = set()


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

    if not one_time_ids and not recurring_ids:
        logger.info("broadcast_tick_completed", due_count=0)
        return

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

    logger.info("broadcast_tick_completed", one_time=len(one_time_ids), recurring=len(recurring_ids))

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


async def dispatch_due_random_replies() -> None:
    """Simplified random-reply toggle: every macro with is_active=True and a
    non-empty message gets one random-reply pass across all of that account's
    groups, on a fixed interval. No per-macro schedule configuration."""
    async with async_session_maker() as db:
        macros = await macro_crud.list_active_with_message(db)

    for macro in macros:
        if macro.id in _running_random_reply:
            continue
        _running_random_reply.add(macro.id)
        try:
            result = await execute_random_reply(macro.id)
            logger.info("random_reply_auto_dispatched", macro_id=macro.id, account_id=macro.account_id, result=result)
        except Exception as exc:
            logger.error("random_reply_auto_dispatch_failed", macro_id=macro.id, error=str(exc))
        finally:
            _running_random_reply.discard(macro.id)


def start_scheduler() -> None:
    scheduler.add_job(
        dispatch_due_broadcasts,
        IntervalTrigger(seconds=DISPATCH_INTERVAL_SECONDS),
        id="dispatch_due_broadcasts",
        replace_existing=True,
    )
    scheduler.add_job(
        check_usdt_payments,
        IntervalTrigger(minutes=5),
        id="check_usdt_payments",
        replace_existing=True,
    )
    scheduler.add_job(
        downgrade_expired_tenants,
        IntervalTrigger(minutes=30),
        id="downgrade_expired_tenants",
        replace_existing=True,
    )
    # 체험 만료 D-1 재참여 DM — 매 시간 체크, 대상자에게 1회만 발송(trial_expiry_notified 가드).
    scheduler.add_job(
        notify_expiring_trials,
        IntervalTrigger(hours=1),
        id="notify_expiring_trials",
        replace_existing=True,
    )
    # Smart Join Queue — tick every DISPATCH_INTERVAL_SECONDS
    scheduler.add_job(
        process_all_accounts,
        IntervalTrigger(seconds=DISPATCH_INTERVAL_SECONDS),
        id="process_join_queue",
        replace_existing=True,
    )
    # AI 운영 자동화 — report-only, no actions taken (see ai_ops_service docstring).
    scheduler.add_job(
        generate_and_store_ops_report,
        IntervalTrigger(hours=24),
        id="generate_ai_ops_report",
        replace_existing=True,
    )
    # 랜덤 답장 on/off 토글 — 켜진 계정은 이 주기로 자동 실행 (대상 그룹은 매번 새로 조회).
    scheduler.add_job(
        dispatch_due_random_replies,
        IntervalTrigger(minutes=RANDOM_REPLY_INTERVAL_MINUTES),
        id="dispatch_due_random_replies",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("scheduler_started", interval_seconds=DISPATCH_INTERVAL_SECONDS)


def shutdown_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("scheduler_stopped")