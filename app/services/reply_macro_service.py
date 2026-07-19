import json
from datetime import datetime, timezone

from sqlalchemy import select

from app.core.logging import get_logger
from app.crud import account as account_crud
from app.crud import reply_macro as macro_crud
from app.database import async_session_maker
from app.models.reply_macro import ReplyMacroLog
from app.services.delivery import DeliveryRequest, DeliveryStatus, deliver_message
from app.services.telegram_actions import AccountNotAuthenticatedError, get_authorized_client

logger = get_logger(__name__)


async def execute_reply_macro(macro_id: str) -> dict:
    """Execute a reply macro.

    Sends ``macro.message_content`` to all ``target_chats`` unless the
    daily ``max_sends_per_day`` limit has already been reached. Each
    successful send is logged in ``ReplyMacroLog`` and the macro's
    ``last_sent_at`` is updated.
    """
    async with async_session_maker() as db:
        macro = await macro_crud.get_macro(db, macro_id)
        if macro is None or not macro.is_active:
            return {"status": "skipped", "reason": "not_found_or_inactive"}

        account = await account_crud.get_account(db, macro.account_id)
        if account is None:
            return {"status": "failed", "reason": "account_not_found"}

        target_chats_raw = macro.target_chats
        target_chats = json.loads(target_chats_raw) if target_chats_raw.startswith("[") else target_chats_raw.split(",")
        target_chats = [c.strip() for c in target_chats if c.strip()]
        if not target_chats:
            return {"status": "skipped", "reason": "no_targets"}

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        if macro.last_sent_at is not None:
            elapsed_hours = (now - macro.last_sent_at).total_seconds() / 3600
            if elapsed_hours < 24 and macro.max_sends_per_day <= 0:
                return {"status": "skipped", "reason": "max_sends_per_day_reached"}

        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        sent_today = await db.execute(
            select(ReplyMacroLog).where(
                ReplyMacroLog.macro_id == macro_id,
                ReplyMacroLog.created_at >= today_start,
                ReplyMacroLog.status == "success",
            )
        )
        sent_today_count = len(sent_today.scalars().all())
        if sent_today_count >= macro.max_sends_per_day:
            return {"status": "skipped", "reason": "max_sends_per_day_reached"}

    try:
        client = await get_authorized_client(account)
    except AccountNotAuthenticatedError:
        return {"status": "failed", "reason": "not_authenticated"}

    results = []
    async with async_session_maker() as db:
        macro = await macro_crud.get_macro(db, macro_id)
        if macro is None:
            return {"status": "failed", "reason": "macro_deleted"}

        for chat_id in target_chats:
            try:
                cleaned = chat_id.lstrip("-")
                target = int(chat_id) if cleaned.isdigit() else chat_id
            except ValueError:
                logger.warning("reply_macro: invalid chat_id %s", chat_id)
                continue

            reply_to_msg_id = getattr(macro, "reply_to_message_id", None)
            request = DeliveryRequest(
                account_id=macro.account_id,
                recipients=[chat_id],
                message=macro.message_content,
                media_path=macro.media_path,
                source="reply_macro",
                source_id=macro.id,
                reply_to_msg_id=reply_to_msg_id,
            )

            try:
                delivery_results = await deliver_message(request)
            except Exception as exc:
                logger.error("reply_macro: delivery failed for %s: %s", chat_id, exc)
                await macro_crud.create_log(
                    db,
                    macro_id=macro.id,
                    account_id=macro.account_id,
                    target_chat_id=chat_id,
                    message_sent=macro.message_content,
                    status="failed",
                    error_message=str(exc),
                )
                results.append({"chat_id": chat_id, "status": "failed", "error": str(exc)})
                continue

            for dr in delivery_results:
                is_success = dr.status == DeliveryStatus.SUCCESS
                await macro_crud.create_log(
                    db,
                    macro_id=macro.id,
                    account_id=macro.account_id,
                    target_chat_id=chat_id,
                    message_sent=macro.message_content,
                    status="success" if is_success else "failed",
                    error_message=dr.error_message if not is_success else None,
                )
                if is_success:
                    results.append({"chat_id": chat_id, "status": "success"})
                else:
                    results.append({"chat_id": chat_id, "status": "failed", "error": dr.error_message})

        await db.refresh(macro)
        macro.last_sent_at = now
        await db.commit()

    return {"status": "completed", "results": results}
