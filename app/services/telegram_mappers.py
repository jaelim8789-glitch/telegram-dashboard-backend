"""Shared Telethon entity -> dict normalizers.

Used by both the REST chat endpoints (app/services/chat_actions.py) and the
realtime update handlers (app/realtime/handlers.py) so a dialog or message
looks identical whether it came from a polled REST call or a live event.
"""

from telethon.tl.types import (
    Dialog, Message, User, Chat, Channel,
    MessageService, MessageMediaPhoto, MessageMediaDocument,
    MessageMediaWebPage,
    PeerUser, PeerChat, PeerChannel,
    UserStatusOnline, UserStatusOffline, UserStatusRecently,
    UserStatusLastWeek, UserStatusLastMonth,
)

from app.core.logging import get_logger

logger = get_logger(__name__)


def peer_id(peer) -> int | None:
    if isinstance(peer, PeerUser):
        return peer.user_id
    elif isinstance(peer, PeerChat):
        return peer.chat_id
    elif isinstance(peer, PeerChannel):
        return peer.channel_id
    return None


def user_online_status(user: User) -> bool | None:
    """Best-effort online status from a Telethon User's `status` field.

    Telegram only reports presence for users you have some form of contact
    with (mutual contacts, recent chats, etc.) — for many users `status` is
    `UserStatusEmpty`/`None` and true presence is unknowable without extra
    per-user calls (e.g. GetFullUser), which we deliberately avoid here to
    not add N+1 requests per dialog. This only surfaces status Telegram
    already includes for free in the `iter_dialogs`/`get_dialogs` response.

    Returns True/False when Telegram gives a concrete online/offline status,
    or None when presence is unknown/hidden (privacy settings, no data,
    non-user entities like groups/channels).
    """
    status = getattr(user, "status", None)
    if isinstance(status, UserStatusOnline):
        return True
    if isinstance(status, (UserStatusOffline, UserStatusRecently, UserStatusLastWeek, UserStatusLastMonth)):
        return False
    return None


def dialog_to_dict(dialog: Dialog) -> dict | None:
    try:
        entity = dialog.entity
        title = ""
        dtype = "unknown"
        username = None
        participants = 0
        is_online = None

        if isinstance(entity, User):
            title = f"{entity.first_name or ''} {entity.last_name or ''}".strip() or entity.username or "Unknown"
            dtype = "private"
            username = entity.username
            is_online = user_online_status(entity)
        elif isinstance(entity, Chat):
            title = entity.title or "Group"
            dtype = "group"
            participants = entity.participants_count or 0
        elif isinstance(entity, Channel):
            title = entity.title or "Channel"
            dtype = "megagroup" if entity.megagroup else "channel"
            username = entity.username
            participants = entity.participants_count or 0

        return {
            "id": entity.id if hasattr(entity, "id") else 0,
            "title": title,
            "type": dtype,
            "unread_count": dialog.unread_count,
            "last_message": (dialog.message.message or "")[:200] if dialog.message and not isinstance(dialog.message, MessageService) else None,
            "last_message_date": dialog.message.date.replace(tzinfo=None).isoformat() if dialog.message and dialog.message.date else None,
            "pinned": dialog.pinned or False,
            "photo": None,
            "participants_count": participants,
            "username": username,
            "is_online": is_online,
        }
    except Exception as exc:
        logger.warning("dialog_to_dict_failed", error=str(exc), dialog_id=getattr(dialog, "id", "unknown"))
        return None


def message_to_dict(msg: Message, my_user_id: int | None = None) -> dict | None:
    if isinstance(msg, MessageService):
        return None  # skip service messages (join, leave, etc.)

    text = msg.message or ""
    media_type = None
    media_file_id = None

    if msg.media:
        if isinstance(msg.media, MessageMediaPhoto):
            media_type = "photo"
        elif isinstance(msg.media, MessageMediaDocument):
            media_type = "document"
        elif isinstance(msg.media, MessageMediaWebPage):
            pass  # webpage preview, keep text
        else:
            media_type = "unknown"

    sender_name = None
    if msg.sender_id and isinstance(msg.sender, User):
        sender_name = f"{msg.sender.first_name or ''} {msg.sender.last_name or ''}".strip() or f"User {msg.sender_id}"
    elif msg.sender_id and isinstance(msg.sender, (Chat, Channel)):
        sender_name = msg.sender.title if hasattr(msg.sender, "title") else f"Chat {msg.sender_id}"

    reply_to_id = getattr(msg, "reply_to", None)
    if reply_to_id and hasattr(reply_to_id, "reply_to_msg_id"):
        reply_to_id = reply_to_id.reply_to_msg_id

    return {
        "id": msg.id,
        "sender_id": msg.sender_id,
        "sender_name": sender_name,
        "text": text,
        "date": msg.date.replace(tzinfo=None).isoformat() if msg.date else None,
        "is_outgoing": my_user_id is not None and msg.sender_id == my_user_id,
        "reply_to_msg_id": reply_to_id,
        "reply_to_text": None,
        "media_type": media_type,
        "media_file_id": media_file_id,
        "is_forwarded": msg.fwd_from is not None,
        "forward_from_name": str(msg.fwd_from.from_name) if msg.fwd_from and msg.fwd_from.from_name else None,
    }
