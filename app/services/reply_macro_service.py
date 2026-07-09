import json
from datetime import date, datetime, timezone

from sqlalchemy import func, select

from app.core.logging import get_logger
from app.crud import account as account_crud
from app.crud import reply_macro as macro_crud
from app.database import async_session_maker
from app.models.reply_macro import ReplyMacroLog
from app.services.delivery import DeliveryRequest, DeliveryStatus, deliver_message
from app.services.telegram_actions import AccountNotAuthenticatedError, get_authorized_client

logger = get_logger(__name__)


async def _count_daily_sends(macro_id: str) -> int:
    """Count how many times this macro has been sent today (UTC)."""
    today_start = datetime.combine(date.today(), datetime.min.time()).replace(tzinfo=None)
    async with async_session_maker() as db:
        result = await db.execute(
            select(func.count(ReplyMacroLog.id)).where(
                ReplyMacroLog.macro_id == macro_id,
                ReplyMacroLog.created_at >= today_start,
            )
        )
        return result.scalar() or 0


async def execute_reply_macro(macro_id: str) -> None:
    """Execute a single Reply Macro using the canonical delivery pipeline.

    Called either manually (via API) or by the scheduler at the macro's interval/fixed time.
    Enforces max_sends_per_day before sending.
    """
    async with async_session_maker() as db:
        macro = await macro_crud.get_macro(db, macro_id)
        if macro is None or not macro.is_active:
            logger.warning("reply_macro_skipped", macro_id=macro_id, reason="not_found_or_inactive")
            return

        account = await account_crud.get_account(db, macro.account_id)
        if account is None:
            logger.error("reply_macro_failed", macro_id=macro_id, reason="account_not_found")
            return

        # Enforce max_sends_per_day
        daily_count = await _count_daily_sends(macro_id)
        if daily_count >= macro.max_sends_per_day:
            logger.warning(
                "reply_macro_skipped_daily_limit",
                macro_id=macro_id, daily_count=daily_count, max_sends_per_day=macro.max_sends_per_day,
            )
            return

    # Parse target chats
    try:
        target_chats = json.loads(macro.target_chats) if macro.target_chats.startswith("[") else macro.target_chats.split(",")
    except (json.JSONDecodeError, AttributeError):
        target_chats = macro.target_chats.split(",")

    target_chats = [c.strip() for c in target_chats if c.strip()]
    if not target_chats:
        logger.warning("reply_macro_skipped", macro_id=macro_id, reason="no_targets")
        return

    # Use canonical delivery pipeline
    request = DeliveryRequest(
        account_id=macro.account_id,
        recipients=target_chats,
        message=macro.message_content,
        media_path=macro.media_path,
        source="reply_macro",
        source_id=macro.id,
    )

    results = await deliver_message(request)

    # Log results via existing ReplyMacroLog for backward compatibility
    async with async_session_maker() as db:
        macro = await macro_crud.get_macro(db, macro_id)
        if macro is None:
            return

        for result in results:
            is_success = result.status == DeliveryStatus.SUCCESS
            await macro_crud.create_log(
                db,
                macro_id=macro.id,
                account_id=macro.account_id,
                target_chat_id=result.recipient,
                message_sent=macro.message_content,
                status="success" if is_success else "failed",
                error_message=result.error_message if not is_success else None,
            )

        await macro_crud.mark_macro_sent(db, macro)

        success_count = sum(1 for r in results if r.status == DeliveryStatus.SUCCESS)
        if success_count == len(results):
            logger.info("reply_macro_completed", macro_id=macro.id, account_id=macro.account_id, targets=len(target_chats))
        else:
            logger.warning("reply_macro_partial", macro_id=macro.id, success=success_count, total=len(target_chats))