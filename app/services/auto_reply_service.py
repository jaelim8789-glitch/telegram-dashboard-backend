from datetime import timedelta
from time import time
import asyncio

from telethon import TelegramClient, events

from app.core.logging import get_logger
from app.crud import account as account_crud
from app.crud import auto_reply as auto_reply_crud
from app.database import async_session_maker
from app.models.auto_reply import AutoReplyRule
from app.models.tenant import Tenant
from app.services.ai_reply_service import record_auto_reply_suggestion
from app.services.delivery import DeliveryRequest, DeliveryStatus, deliver_message
from app.services.ai_spam_guard_service import inspect_and_moderate_message
from app.services.telegram_actions import AccountNotAuthenticatedError, get_authorized_client
from app.services.telethon_pool import pool

logger = get_logger(__name__)

_handlers: dict[str, callable] = {}
_recent_messages: dict[str, float] = {}
_MESSAGE_DEDUP_TTL = 60
_VERIFICATION_INTERVAL = 300
_verification_task = None


def _matches(rule: AutoReplyRule, text: str) -> bool:
    if not text:
        return False
    if rule.match_type == "exact":
        return text.strip() == rule.match_value.strip()
    return rule.match_value.strip().lower() in text.lower()


async def _handle_incoming_message(event, account_id: str) -> None:
    if event.out:
        return

    text = event.raw_text or ""
    if not text:
        return

    event_id = getattr(event, "id", None)
    if event_id is not None:
        msg_key = f"{account_id}:{event.chat_id}:{event_id}"
        now = time()
        if msg_key in _recent_messages and now - _recent_messages[msg_key] < _MESSAGE_DEDUP_TTL:
            return
        _recent_messages[msg_key] = now

    async with async_session_maker() as db:
        account = await account_crud.get_account(db, account_id)
        if account is None or not account.auto_reply_enabled:
            return

        try:
            client = await get_authorized_client(account)
        except AccountNotAuthenticatedError:
            logger.warning("auto_reply_skip_disconnected", account_id=account_id)
            return

        try:
            spam_result = await inspect_and_moderate_message(client, event, account_id)
            if spam_result.is_spam:
                logger.info(
                    "auto_reply_spam_blocked",
                    account_id=account_id,
                    chat_id=event.chat_id,
                    sender_id=event.sender_id,
                    score=spam_result.score,
                    action=spam_result.action_taken,
                )
                return
        except Exception as exc:
            logger.warning("auto_reply_spam_check_failed", account_id=account_id, error=str(exc))

        rules = await auto_reply_crud.list_active_rules(db, account_id)
        matched = next((rule for rule in rules if _matches(rule, text)), None)
        if matched is None:
            if account.ai_fallback_reply_enabled:
                try:
                    fallback_sender = await event.get_sender()
                    await record_auto_reply_suggestion(
                        db,
                        account_id=account_id,
                        chat_id=str(event.chat_id),
                        user_id=str(event.sender_id),
                        user_name=getattr(fallback_sender, "username", None) or getattr(fallback_sender, "first_name", None),
                        trigger_message=text,
                    )
                except Exception as exc:
                    logger.warning("auto_reply_fallback_failed", account_id=account_id, error=str(exc))
            return

        try:
            sender = await event.get_sender()
        except Exception as exc:
            logger.warning("auto_reply_get_sender_failed", account_id=account_id, error=str(exc))
            return

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
            tenant = None
            if account.tenant_id:
                tenant = await db.get(Tenant, account.tenant_id)
            tenant_plan = getattr(tenant, "plan", None)

            request = DeliveryRequest(
                account_id=account_id,
                recipients=[chat_id],
                message=matched.reply_content,
                source="auto_reply",
                source_id=str(matched.id),
                reply_to_msg_id=event.message.id if event.message else None,
                tenant_plan=tenant_plan,
            )
            results = await deliver_message(request, client=client)
            result = results[0] if results else None
            is_success = result is not None and result.status == DeliveryStatus.SUCCESS
            await auto_reply_crud.create_log(
                db,
                rule_id=matched.id,
                account_id=account_id,
                chat_id=chat_id,
                user_id=user_id,
                user_name=user_name,
                trigger_message=text,
                reply_sent=matched.reply_content if is_success else "",
                status="success" if is_success else "failed",
            )
            if is_success:
                logger.info("auto_reply_sent", rule_id=matched.id, account_id=account_id, chat_id=chat_id)
            else:
                logger.error(
                    "auto_reply_failed",
                    rule_id=matched.id,
                    account_id=account_id,
                    chat_id=chat_id,
                    error=result.error_message if result else "no_result",
                )
        except Exception as exc:
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
    auto-reply turned on before the last restart."""
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

    global _verification_task
    if _verification_task is None:
        _verification_task = asyncio.create_task(_verify_listeners_loop())


async def _verify_listeners_loop() -> None:
    while True:
        await asyncio.sleep(_VERIFICATION_INTERVAL)
        await verify_listeners()
        _cleanup_recent_messages()


async def verify_listeners() -> None:
    """Verify all registered listeners are still attached to live, connected clients."""
    async with async_session_maker() as db:
        accounts = await account_crud.list_accounts(db)
    for account in accounts:
        if not account.auto_reply_enabled:
            continue
        client = pool.peek_client(account.id)
        if client is None or not client.is_connected():
            try:
                client = await get_authorized_client(account)
            except AccountNotAuthenticatedError:
                logger.warning("auto_reply_listener_verify_failed", account_id=account.id, reason="unauthenticated")
                continue
            _register(client, account.id)
            logger.info("auto_reply_listener_re_registered", account_id=account.id)


def _cleanup_recent_messages() -> None:
    now = time()
    expired = [k for k, v in _recent_messages.items() if now - v > _MESSAGE_DEDUP_TTL]
    for k in expired:
        del _recent_messages[k]
