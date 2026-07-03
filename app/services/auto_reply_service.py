from datetime import timedelta

from telethon import TelegramClient, events

from app.core.logging import get_logger
from app.crud import account as account_crud
from app.crud import auto_reply as auto_reply_crud
from app.database import async_session_maker
from app.models.auto_reply import AutoReplyRule
from app.services.telegram_actions import AccountNotAuthenticatedError, get_authorized_client
from app.services.telethon_pool import pool

logger = get_logger(__name__)

# One registered Telethon event callback per account_id, so it can be removed again when
# auto-reply is turned off (Telethon has no "handler for this account" lookup of its own —
# add_event_handler/remove_event_handler both take the exact callback object).
_handlers: dict[str, callable] = {}


def _matches(rule: AutoReplyRule, text: str) -> bool:
    if not text:
        return False
    if rule.match_type == "exact":
        return text.strip() == rule.match_value.strip()
    return rule.match_value.strip().lower() in text.lower()


async def _handle_incoming_message(event, account_id: str) -> None:
    if event.out:
        return  # our own sent messages (including auto-replies themselves) — never react to these

    text = event.raw_text or ""
    if not text:
        return

    async with async_session_maker() as db:
        account = await account_crud.get_account(db, account_id)
        if account is None or not account.auto_reply_enabled:
            return

        rules = await auto_reply_crud.list_active_rules(db, account_id)
        matched = next((rule for rule in rules if _matches(rule, text)), None)
        if matched is None:
            return

        sender = await event.get_sender()
        user_id = str(event.sender_id)
        user_name = getattr(sender, "username", None) or getattr(sender, "first_name", None)
        chat_id = str(event.chat_id)

        last_reply_at = await auto_reply_crud.get_last_successful_reply_time(db, matched.id, user_id)
        if last_reply_at is not None:
            elapsed = auto_reply_crud.utcnow_naive() - last_reply_at
            if elapsed < timedelta(hours=matched.cooldown_hours):
                await auto_reply_crud.create_log(
                    db,
                    rule_id=matched.id,
                    account_id=account_id,
                    chat_id=chat_id,
                    user_id=user_id,
                    user_name=user_name,
                    trigger_message=text,
                    reply_sent="",
                    status="rate_limited",
                )
                logger.info("auto_reply_rate_limited", rule_id=matched.id, account_id=account_id, reason="cooldown")
                return

        today_count = await auto_reply_crud.count_successful_replies_today(db, matched.id)
        if today_count >= matched.max_replies_per_day:
            await auto_reply_crud.create_log(
                db,
                rule_id=matched.id,
                account_id=account_id,
                chat_id=chat_id,
                user_id=user_id,
                user_name=user_name,
                trigger_message=text,
                reply_sent="",
                status="rate_limited",
            )
            logger.info("auto_reply_rate_limited", rule_id=matched.id, account_id=account_id, reason="daily_limit")
            return

        try:
            await event.reply(matched.reply_content)
        except Exception as exc:  # noqa: BLE001 — recorded, not swallowed
            await auto_reply_crud.create_log(
                db,
                rule_id=matched.id,
                account_id=account_id,
                chat_id=chat_id,
                user_id=user_id,
                user_name=user_name,
                trigger_message=text,
                reply_sent="",
                status="failed",
            )
            logger.error("auto_reply_failed", rule_id=matched.id, account_id=account_id, error=str(exc))
            return

        await auto_reply_crud.create_log(
            db,
            rule_id=matched.id,
            account_id=account_id,
            chat_id=chat_id,
            user_id=user_id,
            user_name=user_name,
            trigger_message=text,
            reply_sent=matched.reply_content,
            status="success",
        )
        logger.info("auto_reply_sent", rule_id=matched.id, account_id=account_id, chat_id=chat_id)


def _register(client: TelegramClient, account_id: str) -> None:
    if account_id in _handlers:
        return

    async def _callback(event):
        await _handle_incoming_message(event, account_id)

    client.add_event_handler(_callback, events.NewMessage(incoming=True))
    _handlers[account_id] = _callback
    logger.info("auto_reply_listener_attached", account_id=account_id)


def _unregister(client: TelegramClient, account_id: str) -> None:
    callback = _handlers.pop(account_id, None)
    if callback is not None:
        client.remove_event_handler(callback, events.NewMessage)
        logger.info("auto_reply_listener_detached", account_id=account_id)


async def enable_auto_reply(account_id: str) -> None:
    """Connects (or reuses) the account's Telethon client and starts listening.

    Raises AccountNotAuthenticatedError if the account hasn't completed Telegram login yet.
    """
    async with async_session_maker() as db:
        account = await account_crud.get_account(db, account_id)
        if account is None:
            raise AccountNotAuthenticatedError("계정을 찾을 수 없습니다.")
        client = await get_authorized_client(account)
        await account_crud.set_auto_reply_enabled(db, account, True)
    _register(client, account_id)


async def disable_auto_reply(account_id: str) -> None:
    async with async_session_maker() as db:
        account = await account_crud.get_account(db, account_id)
        if account is not None:
            await account_crud.set_auto_reply_enabled(db, account, False)
    client = pool.peek_client(account_id)
    if client is not None:
        _unregister(client, account_id)


async def attach_all_active_listeners() -> None:
    """Called once at app startup: re-attaches listeners for every account that had
    auto-reply turned on before the last restart (the Telethon pool itself is in-memory
    and doesn't survive a restart, so this has to reconnect each client from its stored,
    encrypted session before it can start listening again)."""
    async with async_session_maker() as db:
        accounts = await account_crud.list_accounts(db)
    for account in accounts:
        if not account.auto_reply_enabled:
            continue
        try:
            client = await get_authorized_client(account)
        except AccountNotAuthenticatedError:
            logger.warning("auto_reply_listener_skip_unauthenticated", account_id=account.id)
            continue
        _register(client, account.id)
