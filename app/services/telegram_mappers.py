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
)


def peer_id(peer) -> int | None:
    if isinstance(peer, PeerUser):
        return peer.user_id
    elif isinstance(peer, PeerChat):
        return peer.chat_id
    elif isinstance(peer, PeerChannel):
        return peer.channel_id
    return None


def dialog_to_dict(dialog: Dialog) -> dict:
    entity = dialog.entity
    title = ""
    dtype = "unknown"
    username = None
    participants = 0

    if isinstance(entity, User):
        title = f"{entity.first_name or ''} {entity.last_name or ''}".strip() or entity.username or "Unknown"
        dtype = "private"
        username = entity.username
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
    }


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
