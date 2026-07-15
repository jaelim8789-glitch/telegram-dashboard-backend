import asyncio
from datetime import timedelta

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError

from app.core.logging import get_logger
from app.crud import account as account_crud
from app.crud import auto_reply as auto_reply_crud
from app.database import async_session_maker
from app.models.auto_reply import AutoReplyRule
from app.services.telegram_actions import AccountNotAuthenticatedError, get_authorized_client
from app.services.telethon_pool import pool
from app.services.usage_tracker import record_usage

logger = get_logger(__name__)

# One registered Telethon event callback per account_id, so it can be removed again when
# auto-reply is turned off (Telethon has no "handler for this account" lookup of its own —
# add_event_handler/remove_event_handler both take the exact callback object).
# Each entry stores (callback, client_instance) so the heartbeat can verify the
# client hasn't been evicted from the pool and the connection is still alive.
_handlers: dict[str, tuple[callable, TelegramClient]] = {}


def _matches(rule: AutoReplyRule, text: str) -> bool:
    if not text:
        return False
    if rule.match_type == "exact":
        return text.strip() == rule.match_value.strip()
    return rule.match_value.strip().lower() in text.lower()


MAX_REPLY_RETRIES = 2


async def _send_reply_with_flood_wait_handling(event, reply_content: str) -> bool:
    """Send a reply with FloodWaitError handling and retry.

    Returns True if the reply was sent successfully.
    """
    for attempt in range(1, MAX_REPLY_RETRIES + 1):
        try:
            await event.reply(reply_content)
            return True
        except FloodWaitError as exc:
            wait = min(exc.seconds, 60)
            logger.warning(
                "auto_reply_flood_wait",
                attempt=attempt,
                wait_seconds=wait,
                chat_id=str(event.chat_id),
            )
            if attempt < MAX_REPLY_RETRIES:
                await asyncio.sleep(wait)
            else:
                return False
        except Exception:
            raise


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
            sent = await _send_reply_with_flood_wait_handling(event, matched.reply_content)
            if not sent:
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
                logger.warning("auto_reply_flood_wait_exhausted", rule_id=matched.id, account_id=account_id)
                return
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
        if account.tenant_id:
            await record_usage(account.tenant_id, "auto_reply", 1)
        logger.info("auto_reply_sent", rule_id=matched.id, account_id=account_id, chat_id=chat_id)


def _register(client: TelegramClient, account_id: str) -> None:
    existing = _handlers.get(account_id)
    if existing is not None:
        existing_callback, existing_client = existing
        # If the same client is already registered, nothing to do.
        if existing_client is client:
            return
        # A different client means the old one was evicted — remove its callback first.
        try:
            existing_client.remove_event_handler(existing_callback, events.NewMessage)
        except Exception:
            pass

    async def _callback(event):
        await _handle_incoming_message(event, account_id)

    client.add_event_handler(_callback, events.NewMessage(incoming=True))
    _handlers[account_id] = (_callback, client)
    logger.info("auto_reply_listener_attached", account_id=account_id)


def _unregister(client: TelegramClient, account_id: str) -> None:
    entry = _handlers.pop(account_id, None)
    if entry is not None:
        callback, _ = entry
        try:
            client.remove_event_handler(callback, events.NewMessage)
        except Exception:
            pass
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


async def check_auto_reply_connections() -> None:
    """Heartbeat: verify all auto-reply Telethon client connections are alive.

    Runs periodically (every 60s via the scheduler).  For each account with
    auto-reply enabled:
      1. Check if a client exists in the pool and if it's still connected.
      2. If the client was evicted from the pool (LRU) or disconnected, create
         a fresh authorized client and re-register the event handler.
      3. If the session is expired, log and skip (the account status will be
         reflected in account health).

    This ensures auto-reply survives network blips, Telegram DC migration
    disconnections, and pool LRU eviction without requiring a full process
    restart.
    """
    async with async_session_maker() as db:
        accounts = await account_crud.list_accounts(db)

    for account in accounts:
        if not account.auto_reply_enabled:
            continue

        entry = _handlers.get(account.id)
        if entry is not None:
            _, client = entry
            # Fast path: same client, still connected — nothing to do.
            if client.is_connected():
                continue
            logger.info("auto_reply_client_disconnected", account_id=account.id)

        # Client is missing (evicted) or disconnected — reconnect.
        try:
            fresh_client = await get_authorized_client(account)
        except AccountNotAuthenticatedError:
            logger.warning("auto_reply_reconnect_skip_unauthenticated", account_id=account.id)
            # Remove any stale handler entry so next iteration retries fresh.
            _handlers.pop(account.id, None)
            continue
        except Exception as exc:
            logger.warning("auto_reply_reconnect_failed", account_id=account.id, error=str(exc))
            continue

        _register(fresh_client, account.id)
        logger.info("auto_reply_client_reconnected", account_id=account.id)
