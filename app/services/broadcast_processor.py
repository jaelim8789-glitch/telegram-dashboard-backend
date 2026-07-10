import asyncio

from app.config import settings
from app.core.logging import get_logger
from app.crud import account as account_crud
from app.crud import broadcast as broadcast_crud
from app.database import async_session_maker
from app.services.delivery import DeliveryRequest, deliver_message

logger = get_logger(__name__)


async def process_broadcast(broadcast_id: str) -> None:
    """Runs one broadcast to completion using the canonical delivery pipeline.

    Called either right after creation (FastAPI BackgroundTasks, for immediate sends)
    or by the scheduler once a scheduled broadcast comes due.

    Execution timeout:
      ``deliver_message`` is wrapped in ``asyncio.wait_for`` with a timeout of
      ``settings.broadcast_timeout_seconds`` (default 300 s).  If the timeout fires:
      - the broadcast is persisted with status ``failed`` and a safe error message
      - the ``asyncio.TimeoutError`` is *re-raised* so that the scheduler's
        ``try/except/finally`` can release the in-memory concurrency guard
        (``_running_broadcasts``).
    """
    timeout = settings.broadcast_timeout_seconds

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

        # Re-check the per-account cooldown
        wait_seconds = await broadcast_crud.seconds_until_next_allowed_broadcast(
            db, account.id, exclude_id=broadcast.id
        )
        if wait_seconds > 0:
            await broadcast_crud.update_broadcast_status(
                db, broadcast, status="failed",
                error_message=f"발송 제한: 계정당 1분에 1회로 제한되어 처리하지 못했습니다 "
                f"({int(wait_seconds) + 1}초 후 다시 시도해주세요).",
            )
            logger.warning("broadcast_failed_rate_limited", broadcast_id=broadcast_id, account_id=account.id)
            return

        logger.info("broadcast_started", broadcast_id=broadcast_id, account_id=account.id, recipient_count=len(broadcast.recipients))
        await broadcast_crud.update_broadcast_status(db, broadcast, status="sending", mark_sent=True)

    # Use canonical delivery pipeline with execution timeout
    request = DeliveryRequest(
        account_id=broadcast.account_id,
        recipients=broadcast.recipients,
        message=broadcast.message,
        media_path=broadcast.media_path,
        source="broadcast",
        source_id=broadcast.id,
    )

    try:
        results = await asyncio.wait_for(
            deliver_message(request),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.error(
            "broadcast_timeout",
            broadcast_id=broadcast_id,
            timeout_seconds=timeout,
        )
        async with async_session_maker() as db:
            broadcast = await broadcast_crud.get_broadcast(db, broadcast_id)
            if broadcast is not None:
                await broadcast_crud.update_broadcast_status(
                    db,
                    broadcast,
                    status="failed",
                    error_message=f"발송 시간이 초과되었습니다 ({timeout}초).",
                )
        raise  # re-raise so scheduler's finally discards the in-memory guard

    # Determine overall status
    all_success = all(r.status.value == "success" for r in results)
    any_success = any(r.status.value == "success" for r in results)
    errors = [r.error_message for r in results if r.error_message]

    async with async_session_maker() as db:
        broadcast = await broadcast_crud.get_broadcast(db, broadcast_id)
        if broadcast is None:
            return

        if all_success:
            await broadcast_crud.update_broadcast_status(db, broadcast, status="sent")
            logger.info("broadcast_sent", broadcast_id=broadcast_id, account_id=broadcast.account_id)
        elif any_success:
            await broadcast_crud.update_broadcast_status(
                db, broadcast, status="sent", error_message=f"일부 수신자 전송 실패: {'; '.join(errors[:3])}"
            )
            logger.warning("broadcast_partial", broadcast_id=broadcast_id, errors=errors)
        else:
            await broadcast_crud.update_broadcast_status(
                db, broadcast, status="failed", error_message="; ".join(errors[:3])
            )
            logger.error("broadcast_failed", broadcast_id=broadcast_id, errors=errors)