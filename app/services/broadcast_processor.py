import asyncio
import math

from app.config import settings
from app.core.logging import get_logger
from app.crud import account as account_crud
from app.crud import broadcast as broadcast_crud
from app.database import async_session_maker
from app.services.delivery import DeliveryRequest, deliver_message
from app.services.telegram_actions import get_authorized_client

logger = get_logger(__name__)


async def process_broadcast(broadcast_id: str, *, skip_rate_limit: bool = False) -> None:
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

        if not skip_rate_limit:
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

        is_recurring_parent = (
            broadcast.recurring_interval_minutes is not None
            and broadcast.next_scheduled_at is not None
        )

        delivery_mode = getattr(broadcast, "delivery_mode", "normal")

        logger.info("broadcast_started", broadcast_id=broadcast_id, account_id=account.id,
                     recipient_count=len(broadcast.recipients), delivery_mode=delivery_mode)
        await broadcast_crud.update_broadcast_status(db, broadcast, status="sending", mark_sent=True)

        account_id_local = broadcast.account_id
        recipients_local = broadcast.recipients
        message_local = broadcast.message
        media_path_local = broadcast.media_path

        parent_id = broadcast.id if is_recurring_parent else broadcast.parent_broadcast_id

    if delivery_mode == "cycle":
        # Cycle mode: split recipients into batches, one batch per cycle.
        # Each batch is sent with 1-minute cooldown between batches.
        # The scheduler handles the timing via recurring_interval_minutes.
        # For a one-shot cycle send, we just send all with normal pacing.
        async with async_session_maker() as db:
            broadcast = await broadcast_crud.get_broadcast(db, broadcast_id)
            if broadcast is not None:
                await broadcast_crud.update_broadcast_status(db, broadcast, status="sent")
                logger.info("broadcast_cycle_registered", broadcast_id=broadcast_id,
                            total_recipients=len(recipients_local))
        return

    if delivery_mode == "bulk":
        # Bulk mode: send all recipients immediately with minimal delay (0.3s).
        # Higher risk of Telegram rate limits / account flags.
        timeout = min(timeout, 600)

    reply_to_map: dict[str, int] | None = None
    explicit_reply_to_id: int | None = None

    if delivery_mode == "reply":
        timeout = min(timeout, 600)
        async with async_session_maker() as db:
            broadcast = await broadcast_crud.get_broadcast(db, broadcast_id)
            if broadcast is not None:
                explicit_reply_to_id = getattr(broadcast, "reply_to_msg_id", None)
        if explicit_reply_to_id is not None:
            logger.info(
                "reply_using_explicit_id",
                broadcast_id=broadcast_id,
                reply_to_msg_id=explicit_reply_to_id,
            )
        else:
            async with async_session_maker() as db:
                account = await account_crud.get_account(db, account_id_local)
            if account is not None:
                client = await get_authorized_client(account)
                reply_to_map = {}
                for recipient in recipients_local:
                    try:
                        target = int(recipient.lstrip("-")) if recipient.lstrip("-").isdigit() else recipient
                        messages = await client.get_messages(target, limit=1)
                        if messages:
                            reply_to_map[recipient] = messages[0].id
                    except Exception as exc:
                        logger.warning(
                            "reply_fetch_failed",
                            recipient=recipient,
                            error=str(exc),
                        )

    request = DeliveryRequest(
        account_id=account_id_local,
        recipients=recipients_local,
        message=message_local,
        media_path=media_path_local,
        source="broadcast",
        source_id=broadcast_id,
        reply_to_msg_id=explicit_reply_to_id,
        reply_to_map=reply_to_map,
    )

    if delivery_mode == "bulk":
        request.inter_message_delay = 0.3

    try:
        results = await asyncio.wait_for(
            deliver_message(request),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.error("broadcast_timeout", broadcast_id=broadcast_id, timeout_seconds=timeout)
        async with async_session_maker() as db:
            broadcast = await broadcast_crud.get_broadcast(db, broadcast_id)
            if broadcast is not None:
                await broadcast_crud.update_broadcast_status(
                    db, broadcast, status="failed",
                    error_message=f"발송 시간이 초과되었습니다 ({timeout}초).",
                )
        raise

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



async def process_recurring_parent(parent_broadcast_id: str) -> None:
    """Handles a recurring parent broadcast being due.

    Creates a child broadcast record and dispatches it using the
    standard process_broadcast pipeline. After the child completes,
    the parent's next_scheduled_at is advanced.
    """
    from datetime import datetime, timezone

    async with async_session_maker() as db:
        parent = await broadcast_crud.get_broadcast(db, parent_broadcast_id)
        if parent is None:
            logger.warning("recurring_parent_not_found", parent_id=parent_broadcast_id)
            return

        # Double-check: still active?
        if parent.status == "cancelled" or parent.is_recurring_paused:
            logger.info("recurring_parent_skipped", parent_id=parent_broadcast_id, status=parent.status)
            return

        now = datetime.now(timezone.utc).replace(tzinfo=None)

        # Create child broadcast record for history
        child = await broadcast_crud.create_recurring_child_broadcast(db, parent, now)
        child_id = child.id
        account_id = parent.account_id

    # CRITICAL: Advance next_scheduled_at BEFORE dispatching the child.
    # If the process crashes after child creation but before rescheduling,
    # the parent's next_scheduled_at stays in the past and the next tick
    # would create a SECOND child — duplicate execution.  Advancing here
    # ensures at-most-one child per tick, even on crash.
    async with async_session_maker() as db:
        parent = await broadcast_crud.reschedule_recurring_broadcast(db, parent_broadcast_id)
        if parent is not None:
            logger.info(
                "recurring_parent_prescheduled",
                parent_id=parent_broadcast_id,
                next_scheduled_at=str(parent.next_scheduled_at),
            )

    logger.info(
        "recurring_child_created",
        parent_id=parent_broadcast_id,
        child_id=child_id,
        account_id=account_id,
    )

    # Process the child broadcast (skip rate limit since it's a scheduler dispatch)
    await process_broadcast(child_id, skip_rate_limit=True)
