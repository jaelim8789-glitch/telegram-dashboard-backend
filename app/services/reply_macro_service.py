import json

from app.core.logging import get_logger
from app.crud import account as account_crud
from app.crud import reply_macro as macro_crud
from app.database import async_session_maker
from app.services.telegram_actions import AccountNotAuthenticatedError, get_authorized_client

logger = get_logger(__name__)


async def execute_reply_macro(macro_id: str) -> None:
    """Execute a single Reply Macro: send the canned message to all target chats.
    
    Called either manually (via API) or by the scheduler at the macro's interval/fixed time.
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

        try:
            client = await get_authorized_client(account)
        except AccountNotAuthenticatedError:
            logger.error("reply_macro_failed", macro_id=macro_id, reason="account_not_authenticated")
            return

        # Parse target chats
        try:
            target_chats = json.loads(macro.target_chats) if macro.target_chats.startswith("[") else macro.target_chats.split(",")
        except (json.JSONDecodeError, AttributeError):
            target_chats = macro.target_chats.split(",")

        target_chats = [c.strip() for c in target_chats if c.strip()]
        errors: list[str] = []

        for chat_id in target_chats:
            try:
                if macro.media_path:
                    await client.send_file(chat_id, macro.media_path, caption=macro.message_content)
                else:
                    await client.send_message(chat_id, macro.message_content)

                await macro_crud.create_log(
                    db,
                    macro_id=macro.id,
                    account_id=macro.account_id,
                    target_chat_id=chat_id,
                    message_sent=macro.message_content,
                    status="success",
                )
                logger.info("reply_macro_sent", macro_id=macro.id, account_id=macro.account_id, chat_id=chat_id)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{chat_id}: {exc}")
                await macro_crud.create_log(
                    db,
                    macro_id=macro.id,
                    account_id=macro.account_id,
                    target_chat_id=chat_id,
                    message_sent=macro.message_content,
                    status="failed",
                    error_message=str(exc),
                )
                logger.error("reply_macro_failed", macro_id=macro.id, account_id=macro.account_id, chat_id=chat_id, error=str(exc))

        await macro_crud.mark_macro_sent(db, macro)

        if errors:
            logger.warning("reply_macro_partial_failures", macro_id=macro.id, errors=errors)
        else:
            logger.info("reply_macro_completed", macro_id=macro.id, account_id=macro.account_id, targets=len(target_chats))