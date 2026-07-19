import asyncio
import math

from app.config import settings
from app.core.logging import get_logger
from app.crud import account as account_crud
from app.crud import broadcast as broadcast_crud
from app.database import async_session_maker
from app.services.delivery import DeliveryRequest, deliver_message
from app.services.telegram_actions import get_authorized_client, list_group_members

logger = get_logger(__name__)


async def resolve_group_ids_to_recipients(account, group_ids: list[str]) -> list[str]:
    """Resolve Telegram group chat IDs to member user IDs for broadcast sending.

    For each group, fetches the participant list and returns all member chat IDs
    as strings. Groups the account cannot access are silently skipped with a warning.
    """
    all_members: set[str] = set()
    for gid in group_ids:
        try:
            members = await list_group_members(account, gid)
            for member in members:
                member_id = str(member.id)
                all_members.add(member_id)
            logger.info("group_resolved", group_id=gid, member_count=len(members))
        except Exception as exc:
            logger.warning("group_resolve_failed", group_id=gid, error=str(exc))
            # Skip groups that can't be resolved — continue with others
            continue
    return list(all_members)


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

        # ── Send-to-Group resolution ──────────────────────────────────────
        group_ids = getattr(broadcast, "group_ids", None)
        if group_ids and not broadcast.groups_resolved:
            # Resolve group members to recipient list
            resolved = await resolve_group_ids_to_recipients(account, group_ids)
            if not resolved:
                await broadcast_crud.update_broadcast_status(
                    db, broadcast, status="failed",
                    error_message="그룹에서 발송 대상을 찾을 수 없습니다. 그룹 접근 권한을 확인해주세요.",
                )
                logger.error("broadcast_failed_no_group_members", broadcast_id=broadcast_id, group_ids=group_ids)
                return
            broadcast.recipients = resolved
            broadcast.groups_resolved = True
            await db.commit()
            await db.refresh(broadcast)
            logger.info(
                "broadcast_groups_resolved",
                broadcast_id=broadcast_id,
                group_ids=group_ids,
                resolved_count=len(resolved),
            )

        is_recurring_parent = (
            broadcast.recurring_interval_minutes is not None
            and broadcast.next_scheduled_at is not None
        )

        delivery_mode = getattr(broadcast, "delivery_mode", "normal")

        all_recipients_local = broadcast.recipients
        already_succeeded = await broadcast_crud.get_succeeded_recipients(db, broadcast_id)
        recipients_local = (
            [r for r in all_recipients_local if r not in already_succeeded]
            if already_succeeded else all_recipients_local
        )

        if already_succeeded and not recipients_local:
            await broadcast_crud.update_broadcast_status(db, broadcast, status="sent")
            logger.info(
                "broadcast_already_fully_delivered",
                broadcast_id=broadcast_id,
                recipient_count=len(all_recipients_local),
            )
            return

        logger.info("broadcast_started", broadcast_id=broadcast_id, account_id=account.id,
                     recipient_count=len(recipients_local), delivery_mode=delivery_mode,
                     already_succeeded_count=len(already_succeeded))
        await broadcast_crud.update_broadcast_status(db, broadcast, status="sending", mark_sent=True)

        account_id_local = broadcast.account_id
        message_local = broadcast.message
        media_path_local = broadcast.media_path
        delay_seconds_local = getattr(broadcast, "delay_seconds", None)
        inline_buttons_local = getattr(broadcast, "inline_buttons", None)

        parent_id = broadcast.id if is_recurring_parent else broadcast.parent_broadcast_id

    if delivery_mode == "cycle":
        async with async_session_maker() as db:
            broadcast = await broadcast_crud.get_broadcast(db, broadcast_id)
            if broadcast is not None:
                await broadcast_crud.update_broadcast_status(db, broadcast, status="sent")
                logger.info("broadcast_cycle_registered", broadcast_id=broadcast_id,
                            total_recipients=len(recipients_local))
        return

    if delivery_mode == "bulk":
        timeout = min(timeout, 600)

    reply_to_map: dict[str, int] | None = None
    explicit_reply_to_id: int | None = None
    pre_fetched_client = None

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
            reply_to_map = {recipient: explicit_reply_to_id for recipient in recipients_local}
        else:
            async with async_session_maker() as db:
                account = await account_crud.get_account(db, account_id_local)
            if account is not None:
                pre_fetched_client = await get_authorized_client(account)
                reply_to_map = {}
                for recipient in recipients_local:
                    try:
                        target = int(recipient.lstrip("-")) if recipient.lstrip("-").isdigit() else recipient
                        messages = await pre_fetched_client.get_messages(target, limit=1)
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
        inline_buttons=inline_buttons_local,
    )

    if delivery_mode == "bulk":
        request.inter_message_delay = 0.3
    elif delivery_mode == "normal" and delay_seconds_local is not None:
        request.inter_message_delay = float(delay_seconds_local)

    try:
        results = await asyncio.wait_for(
            deliver_message(request, client=pre_fetched_client),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.error("broadcast_timeout", broadcast_id=broadcast_id, timeout_seconds=timeout)
        total = len(all_recipients_local)
        async with async_session_maker() as db:
            broadcast = await broadcast_crud.get_broadcast(db, broadcast_id)
            if broadcast is not None:
                any_success, all_success, succeeded_count = await broadcast_crud.summarize_message_log_outcomes(
                    db, broadcast_id, total
                )
                if all_success:
                    await broadcast_crud.update_broadcast_status(
                        db, broadcast, status="sent",
                        error_message=f"발송은 완료됐으나 상태 확인이 {timeout}초를 초과했습니다.",
                    )
                elif any_success:
                    await broadcast_crud.update_broadcast_status(
                        db, broadcast, status="sent",
                        error_message=(
                            f"{succeeded_count}/{total}명에게 발송 후 {timeout}초 시간 초과 — "
                            "나머지 수신자는 처리되지 못했습니다."
                        ),
                    )
                else:
                    await broadcast_crud.update_broadcast_status(
                        db, broadcast, status="failed",
                        error_message=f"발송 시간이 초과되었습니다 ({timeout}초). 처리된 수신자: 0/{total}.",
                    )
        raise

    all_success = all(r.status.value == "success" for r in results)
    any_success = any(r.status.value == "success" for r in results) or bool(already_succeeded)
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
    from datetime import datetime, timezone

    async with async_session_maker() as db:
        parent = await broadcast_crud.get_broadcast(db, parent_broadcast_id)
        if parent is None:
            logger.warning("recurring_parent_not_found", parent_id=parent_broadcast_id)
            return

        if parent.status == "cancelled" or parent.is_recurring_paused:
            logger.info("recurring_parent_skipped", parent_id=parent_broadcast_id, status=parent.status)
            return

        now = datetime.now(timezone.utc).replace(tzinfo=None)

        child = await broadcast_crud.create_recurring_child_broadcast(db, parent, now)
        child_id = child.id
        account_id = parent.account_id

    logger.info(
        "recurring_child_created",
        parent_id=parent_broadcast_id,
        child_id=child_id,
        account_id=account_id,
    )

    await process_broadcast(child_id, skip_rate_limit=True)

    async with async_session_maker() as db:
        parent = await broadcast_crud.reschedule_recurring_broadcast(db, parent_broadcast_id)
        if parent is not None:
            logger.info(
                "recurring_parent_prescheduled",
                parent_id=parent_broadcast_id,
                next_scheduled_at=str(parent.next_scheduled_at),
            )