from app.core.logging import get_logger
from app.crud import account as account_crud
from app.crud import broadcast as broadcast_crud
from app.database import async_session_maker
from app.services.telegram_actions import AccountNotAuthenticatedError, run_broadcast

logger = get_logger(__name__)


async def process_broadcast(broadcast_id: str) -> None:
    """Runs one broadcast to completion in-process: re-checks the rate limit, sends,
    records the result. Called either right after creation (FastAPI BackgroundTasks,
    for immediate sends) or by the scheduler once a scheduled broadcast comes due.

    No separate queue/worker process — this app is small enough (1-3 personal accounts,
    max 10 recipients, 1 send/min) that running sends inline is simpler and, on a
    free/sleep-on-idle host, more reliable than depending on an always-on worker that
    such hosts typically don't offer for free.
    """
    async with async_session_maker() as db:
        broadcast = await broadcast_crud.get_broadcast(db, broadcast_id)
        if broadcast is None:
            logger.warning("broadcast_not_found", broadcast_id=broadcast_id)
            return

        account = await account_crud.get_account(db, broadcast.account_id)
        if account is None:
            await broadcast_crud.update_broadcast_status(
                db, broadcast, status="failed", error_message="계정을 찾을 수 없습니다."
            )
            logger.error("broadcast_failed", broadcast_id=broadcast_id, reason="account_not_found")
            return

        # Re-check the per-account cooldown right before actually sending — a burst of
        # immediate requests can land closer together than the creation-time check alone
        # would catch. (The scheduler does its own check before ever calling this, so a
        # scheduled broadcast that's still rate-limited is simply left "pending" for the
        # next tick instead of reaching here — see app/scheduler/scheduler.py.)
        wait_seconds = await broadcast_crud.seconds_until_next_allowed_broadcast(
            db, account.id, exclude_id=broadcast.id
        )
        if wait_seconds > 0:
            await broadcast_crud.update_broadcast_status(
                db,
                broadcast,
                status="failed",
                error_message=f"발송 제한: 계정당 1분에 1회로 제한되어 처리하지 못했습니다 "
                f"({int(wait_seconds) + 1}초 후 다시 시도해주세요).",
            )
            logger.warning(
                "broadcast_failed_rate_limited",
                broadcast_id=broadcast_id,
                account_id=account.id,
                wait_seconds=round(wait_seconds, 1),
            )
            return

        logger.info(
            "broadcast_started",
            broadcast_id=broadcast_id,
            account_id=account.id,
            recipient_count=len(broadcast.recipients),
        )
        await broadcast_crud.update_broadcast_status(db, broadcast, status="sending", mark_sent=True)

        try:
            success, error_message = await run_broadcast(
                account, broadcast.recipients, broadcast.message, broadcast.media_path
            )
        except (AccountNotAuthenticatedError, RuntimeError) as exc:
            success, error_message = False, str(exc)

        await broadcast_crud.update_broadcast_status(
            db,
            broadcast,
            status="sent" if success else "failed",
            error_message=error_message,
        )

        if success:
            logger.info("broadcast_sent", broadcast_id=broadcast_id, account_id=account.id)
        else:
            logger.error(
                "broadcast_failed", broadcast_id=broadcast_id, account_id=account.id, error=error_message
            )
