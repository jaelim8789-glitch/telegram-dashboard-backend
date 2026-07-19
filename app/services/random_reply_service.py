import json
import random
from datetime import datetime, timezone

from app.core.logging import get_logger
from app.crud import account as account_crud
from app.crud import reply_macro as macro_crud
from app.database import async_session_maker
from app.services.delivery import DeliveryRequest, DeliveryStatus, deliver_message
from app.services.telegram_actions import AccountNotAuthenticatedError, get_authorized_client

logger = get_logger(__name__)


async def execute_random_reply(macro_id: str) -> dict:
    """Execute a random reply macro.

    For each target chat, fetch recent messages, pick a random one from a unique user
    who hasn't been replied to before, and send the macro's message as a reply.
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

        used = await macro_crud.get_used_targets(macro)
        used_set = {(u["chat_id"], u["user_id"]) for u in used}

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
                messages = await client.get_messages(target, limit=20)
            except Exception as exc:
                logger.warning("random_reply: failed to fetch messages for %s: %s", chat_id, exc)
                continue

            # Pick a random message from a unique user not already used
            candidates = []
            seen_users_in_chat = set()
            for msg in messages:
                if msg.out:
                    continue  # skip our own messages
                sender = await msg.get_sender()
                if sender is None:
                    continue
                uid = str(sender.id)
                if (chat_id, uid) in used_set:
                    continue
                if uid in seen_users_in_chat:
                    continue  # only one msg per user per chat
                seen_users_in_chat.add(uid)
                candidates.append((uid, msg))

            if not candidates:
                logger.info("random_reply: no candidates in %s (all used)", chat_id)
                continue

            chosen_uid, chosen_msg = random.choice(candidates)

            # Send as reply to the chosen message
            request = DeliveryRequest(
                account_id=macro.account_id,
                recipients=[chat_id],
                message=macro.message_content,
                media_path=macro.media_path,
                source="random_reply",
                source_id=macro.id,
                reply_to_map={chat_id: chosen_msg.id},
            )

            delivery_results = await deliver_message(request)

            for dr in delivery_results:
                is_success = dr.status == DeliveryStatus.SUCCESS
                await macro_crud.create_log(
                    db,
                    macro_id=macro.id,
                    account_id=macro.account_id,
                    target_chat_id=chat_id,
                    replied_user_id=chosen_uid,
                    replied_msg_id=chosen_msg.id,
                    message_sent=macro.message_content,
                    status="success" if is_success else "failed",
                    error_message=dr.error_message if not is_success else None,
                )
                if is_success:
                    await macro_crud.add_used_target(db, macro, chat_id, chosen_uid)
                results.append({
                    "chat_id": chat_id,
                    "user_id": chosen_uid,
                    "status": "success" if is_success else "failed",
                })

        await macro_crud.mark_macro_sent(db, macro)

    return {"status": "completed", "results": results}