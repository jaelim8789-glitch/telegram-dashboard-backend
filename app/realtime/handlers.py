"""Attaches one multiplexed set of Telethon event handlers per pooled account.

Each callback does the minimum work needed to normalize the update into an
`UpdateEvent` and hands it to the dispatcher — no awaiting on DB/network work
here, see dispatcher.py for why.

Covers the update categories that matter for keeping the chat UI in sync:
new/edited/deleted messages, read receipts, typing + online presence, and
chat-membership actions (join/leave/pin/title change). This does not attempt
to enumerate Telegram's full raw update surface (events.Raw) — the high-level
Telethon event classes already collapse the ~100+ MTProto update types into
these buckets, which is the same abstraction level the rest of this codebase
(chat_actions.py) is built on.
"""

from telethon import TelegramClient, events, utils

from app.core.logging import get_logger
from app.realtime.dispatcher import UpdateEvent, dispatcher
from app.services.telegram_mappers import message_to_dict

logger = get_logger(__name__)


def _raw_chat_id(chat_id: int | None) -> int | None:
    """Telethon's `event.chat_id` is the "marked" ID (users unchanged, basic
    groups negated, channels/supergroups prefixed with -100) — but
    `telegram_mappers.dialog_to_dict()` (what the REST dialogs list and the
    frontend's `room.id` are built from) uses the raw, unmarked `entity.id`.
    Publishing the marked ID here meant every group/channel event silently
    failed to match any open chat room on the frontend (chat_id !== room.id),
    so live updates never rendered — only a refresh (which re-fetches via
    REST with the raw ID) ever showed them. Private 1:1 chats happened to
    still work since a user ID is identical in both forms.
    """
    if chat_id is None:
        return None
    return utils.resolve_id(chat_id)[0]

# account_id -> list of (event_class, callback) so unregister can remove exactly
# what register attached, mirroring auto_reply_service.py's _handlers pattern.
_registered: dict[str, list[tuple[type, callable]]] = {}


def register_account_realtime(client: TelegramClient, account_id: str, my_user_id: int | None) -> None:
    if account_id in _registered:
        return

    handlers: list[tuple[type, callable]] = []

    # my_user_id is resolved ONCE by the caller (attach_all_account_realtime_listeners)
    # rather than re-fetched here. This used to call `await client.get_me()` on
    # every single new/edited message event — under a burst of incoming
    # messages (e.g. catching up on updates right after login) that fired
    # dozens of redundant GetUsersRequest RPCs per second, which Telegram
    # started flood-waiting (observed: "A wait of 3 seconds is required
    # (caused by GetUsersRequest)" repeated continuously), stalling the
    # entire realtime pipeline for the account. The caller's own user ID
    # never changes for the lifetime of a session, so there's nothing to
    # re-fetch per event.
    async def on_new_message(event):
        try:
            m = message_to_dict(event.message, my_user_id)
        except Exception as exc:
            logger.warning("realtime_new_message_normalize_failed", account_id=account_id, error=str(exc))
            return
        if m is None:
            return
        dispatcher.publish(UpdateEvent(
            account_id=account_id,
            event_type="message.new",
            chat_id=_raw_chat_id(event.chat_id),
            payload={"message": m},
        ))

    async def on_message_edited(event):
        try:
            m = message_to_dict(event.message, my_user_id)
        except Exception as exc:
            logger.warning("realtime_message_edited_normalize_failed", account_id=account_id, error=str(exc))
            return
        if m is None:
            return
        dispatcher.publish(UpdateEvent(
            account_id=account_id,
            event_type="message.edited",
            chat_id=_raw_chat_id(event.chat_id),
            payload={"message": m},
        ))

    async def on_message_deleted(event):
        dispatcher.publish(UpdateEvent(
            account_id=account_id,
            event_type="message.deleted",
            chat_id=_raw_chat_id(event.chat_id),
            payload={"deleted_ids": list(event.deleted_ids)},
        ))

    async def on_message_read(event):
        dispatcher.publish(UpdateEvent(
            account_id=account_id,
            event_type="message.read",
            chat_id=_raw_chat_id(event.chat_id),
            payload={
                "max_id": event.max_id,
                "inbox": event.inbox,
                "outbox": event.outbox,
            },
        ))

    async def on_user_update(event):
        # Covers both presence changes (online/offline/last-seen) and typing/
        # recording/uploading actions — Telethon folds UpdateUserStatus and
        # UpdateUserTyping/UpdateChatUserTyping into the same event class.
        payload: dict = {"user_id": event.user_id}
        if getattr(event, "typing", False):
            event_type = "chat.typing"
            payload["action"] = "typing"
        elif event.status is not None:
            event_type = "user.status"
            payload["online"] = bool(getattr(event, "online", False))
            payload["last_seen"] = getattr(getattr(event, "status", None), "was_online", None)
            if payload["last_seen"] is not None:
                payload["last_seen"] = payload["last_seen"].replace(tzinfo=None).isoformat()
        else:
            event_type = "chat.typing"
            payload["action"] = "action"
        dispatcher.publish(UpdateEvent(
            account_id=account_id,
            event_type=event_type,
            chat_id=_raw_chat_id(getattr(event, "chat_id", None)),
            payload=payload,
        ))

    async def on_chat_action(event):
        dispatcher.publish(UpdateEvent(
            account_id=account_id,
            event_type="chat.action",
            chat_id=_raw_chat_id(event.chat_id),
            payload={
                "user_added": bool(getattr(event, "user_added", False)),
                "user_joined": bool(getattr(event, "user_joined", False)),
                "user_left": bool(getattr(event, "user_left", False)),
                "user_kicked": bool(getattr(event, "user_kicked", False)),
                "new_pin": bool(getattr(event, "new_pin", False)),
                "new_title": getattr(event, "new_title", None),
            },
        ))

    handlers.append((events.NewMessage, on_new_message))
    handlers.append((events.MessageEdited, on_message_edited))
    handlers.append((events.MessageDeleted, on_message_deleted))
    handlers.append((events.MessageRead, on_message_read))
    handlers.append((events.UserUpdate, on_user_update))
    handlers.append((events.ChatAction, on_chat_action))

    for event_class, callback in handlers:
        client.add_event_handler(callback, event_class())

    _registered[account_id] = handlers
    logger.info("realtime_listener_attached", account_id=account_id, handler_count=len(handlers))


def unregister_account_realtime(client: TelegramClient, account_id: str) -> None:
    handlers = _registered.pop(account_id, None)
    if not handlers:
        return
    for event_class, callback in handlers:
        client.remove_event_handler(callback, event_class)
    logger.info("realtime_listener_detached", account_id=account_id)


async def attach_all_account_realtime_listeners() -> None:
    """Called once at app startup (behind the realtime_update_handlers singleton
    lock — see app/main.py) to re-attach live listeners for every account whose
    Telegram session is already authenticated. Unlike auto-reply's listener,
    this is not opt-in per account: any active, authenticated account should
    push live updates to its own chat UI."""
    from app.database import async_session_maker
    from app.crud import account as account_crud
    from app.services.telegram_actions import get_authorized_client, AccountNotAuthenticatedError

    async with async_session_maker() as db:
        accounts = await account_crud.list_accounts(db)

    for account in accounts:
        if account.status != "active":
            continue
        try:
            client = await get_authorized_client(account)
            me = await client.get_me()
        except AccountNotAuthenticatedError:
            logger.info("realtime_listener_skip_unauthenticated", account_id=account.id)
            continue
        except Exception as exc:
            logger.warning("realtime_listener_attach_failed", account_id=account.id, error=str(exc))
            continue
        register_account_realtime(client, account.id, me.id if me else None)
