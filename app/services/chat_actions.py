"""Telegram chat operations: list dialogs, fetch messages, send messages, live SSE."""

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, AsyncGenerator

from telethon.tl.types import (
    Dialog, Message, User, Chat, Channel,
    MessageService, MessageMediaPhoto, MessageMediaDocument,
    MessageMediaWebPage,
    PeerUser, PeerChat, PeerChannel,
)

from app.core.logging import get_logger
from app.database import async_session_maker
from app.crud import account as account_crud
from app.services.telegram_actions import get_authorized_client, AccountNotAuthenticatedError

logger = get_logger(__name__)


def _peer_id(peer) -> int | None:
    if isinstance(peer, PeerUser):
        return peer.user_id
    elif isinstance(peer, PeerChat):
        return peer.chat_id
    elif isinstance(peer, PeerChannel):
        return peer.channel_id
    return None


def _dialog_to_dict(dialog: Dialog) -> dict:
    entity = dialog.entity
    title = ""
    dtype = "unknown"
    username = None
    photo = None
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
        "last_message": dialog.message.message[:200] if dialog.message and not isinstance(dialog.message, MessageService) else None,
        "last_message_date": dialog.message.date.replace(tzinfo=None).isoformat() if dialog.message and dialog.message.date else None,
        "pinned": dialog.pinned or False,
        "photo": None,
        "participants_count": participants,
        "username": username,
    }


def _message_to_dict(msg: Message, my_user_id: int | None = None) -> dict:
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

    reply_text = None
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
        "reply_to_text": reply_text,
        "media_type": media_type,
        "media_file_id": media_file_id,
        "is_forwarded": msg.fwd_from is not None,
        "forward_from_name": str(msg.fwd_from.from_name) if msg.fwd_from and msg.fwd_from.from_name else None,
    }


async def list_dialogs(account_id: str, limit: int = 100) -> list[dict]:
    """List all Telegram dialogs (1:1, groups, channels) for an account."""
    async with async_session_maker() as db:
        account = await account_crud.get_account(db, account_id)
        if account is None:
            raise ValueError("Account not found")
    client = await get_authorized_client(account)
    me = await client.get_me()
    my_user_id = me.id if me else None

    dialogs = []
    async for dialog in client.iter_dialogs(limit=limit):
        d = _dialog_to_dict(dialog)
        if d:
            dialogs.append(d)
    return dialogs


async def fetch_messages(
    account_id: str,
    chat_id: int,
    limit: int = 50,
    offset_id: int | None = None,
) -> list[dict]:
    """Fetch messages from a specific chat."""
    async with async_session_maker() as db:
        account = await account_crud.get_account(db, account_id)
        if account is None:
            raise ValueError("Account not found")
    client = await get_authorized_client(account)
    me = await client.get_me()
    my_user_id = me.id if me else None

    kwargs = {"limit": limit}
    if offset_id:
        kwargs["offset_id"] = offset_id

    messages = await client.get_messages(chat_id, **kwargs)
    result = []
    for msg in messages:
        m = _message_to_dict(msg, my_user_id)
        if m:
            result.append(m)
    # Get reply texts for messages that have reply_to
    reply_ids = {m["reply_to_msg_id"] for m in result if m["reply_to_msg_id"]}
    if reply_ids:
        reply_map = {}
        for reply_id in reply_ids:
            reply_msg = await client.get_messages(chat_id, ids=reply_id)
            if reply_msg and not isinstance(reply_msg, MessageService):
                reply_map[reply_id] = reply_msg.message[:200]
        for m in result:
            if m["reply_to_msg_id"] in reply_map:
                m["reply_to_text"] = reply_map[m["reply_to_msg_id"]]
    return result


async def send_chat_message(
    account_id: str,
    chat_id: int,
    text: str,
    reply_to_msg_id: int | None = None,
    media_path: str | None = None,
) -> dict:
    """Send a message to a Telegram chat."""
    async with async_session_maker() as db:
        account = await account_crud.get_account(db, account_id)
        if account is None:
            raise ValueError("Account not found")
    client = await get_authorized_client(account)

    kwargs = {"comment_to": chat_id} if chat_id < 0 and abs(chat_id) > 10**12 else {}
    if media_path:
        sent = await client.send_file(chat_id, media_path, caption=text, reply_to=reply_to_msg_id or None)
    else:
        sent = await client.send_message(chat_id, text, reply_to=reply_to_msg_id or None)

    msg_id = sent.id if hasattr(sent, "id") else 0
    return {"message_id": msg_id, "status": "sent"}


async def send_typing_indicator(account_id: str, chat_id: int, typing: bool = True):
    """Send typing indicator to a Telegram chat."""
    async with async_session_maker() as db:
        account = await account_crud.get_account(db, account_id)
        if account is None:
            raise ValueError("Account not found")
    client = await get_authorized_client(account)

    from telethon.tl.functions.messages import SetTypingRequest
    from telethon.tl.types import SendMessageTypingAction, SendMessageCancelAction

    action = SendMessageTypingAction() if typing else SendMessageCancelAction()
    try:
        await client(SetTypingRequest(peer=chat_id, action=action))
    except Exception as e:
        logger.warning("typing_indicator_failed", chat_id=chat_id, error=str(e))


async def mute_dialog(account_id: str, chat_id: int, mute: bool = True):
    """Mute/unmute a Telegram dialog."""
    from datetime import timedelta

    async with async_session_maker() as db:
        account = await account_crud.get_account(db, account_id)
        if account is None:
            raise ValueError("Account not found")
    client = await get_authorized_client(account)

    from telethon.tl.functions.account import UpdateNotifySettingsRequest
    from telethon.tl.types import InputPeerNotifySettings, InputNotifyPeer

    peer = await client.get_input_entity(chat_id)
    if mute:
        await client(UpdateNotifySettingsRequest(
            peer=InputNotifyPeer(peer=peer),
            settings=InputPeerNotifySettings(mute_until=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=365))
        ))
    else:
        await client(UpdateNotifySettingsRequest(
            peer=InputNotifyPeer(peer=peer),
            settings=InputPeerNotifySettings(mute_until=0)
        ))
    return {"status": "muted" if mute else "unmuted"}


async def pin_dialog(account_id: str, chat_id: int, pin: bool = True):
    """Pin/unpin a Telegram dialog."""
    async with async_session_maker() as db:
        account = await account_crud.get_account(db, account_id)
        if account is None:
            raise ValueError("Account not found")
    client = await get_authorized_client(account)

    from telethon.tl.functions.messages import ToggleDialogPinRequest
    from telethon.tl.types import InputDialogPeer

    peer = await client.get_input_entity(chat_id)
    await client(ToggleDialogPinRequest(
        peer=InputDialogPeer(peer=peer),
        pinned=pin
    ))
    return {"status": "pinned" if pin else "unpinned"}


async def delete_dialog(account_id: str, chat_id: int):
    """Delete a Telegram dialog."""
    async with async_session_maker() as db:
        account = await account_crud.get_account(db, account_id)
        if account is None:
            raise ValueError("Account not found")
    client = await get_authorized_client(account)

    from telethon.tl.functions.messages import DeleteHistoryRequest

    await client(DeleteHistoryRequest(peer=chat_id, max_id=0, just_clear=False, revoke=True))
    return {"status": "deleted"}


async def stream_new_messages(
    account_id: str,
    chat_id: int,
) -> AsyncGenerator[str, None]:
    """SSE generator that yields new messages as they arrive."""
    async with async_session_maker() as db:
        account = await account_crud.get_account(db, account_id)
        if account is None:
            yield f"event: error\ndata: Account not found\n\n"
            return
    try:
        client = await get_authorized_client(account)
        me = await client.get_me()
        my_user_id = me.id if me else None
    except Exception as e:
        yield f"event: error\ndata: {str(e)}\n\n"
        return

    last_id = 0  # will be set from initial fetch

    while True:
        try:
            messages = await client.get_messages(chat_id, limit=5)
            for msg in reversed(messages):
                m = _message_to_dict(msg, my_user_id)
                if m and m["id"] > last_id:
                    yield f"event: message\ndata: {json.dumps(m)}\n\n"
                    last_id = m["id"]
            await asyncio.sleep(3)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("chat_stream_error", chat_id=chat_id, error=str(e))
            yield f"event: error\ndata: {str(e)}\n\n"
            await asyncio.sleep(5)


async def search_messages(
    account_id: str,
    query: str,
    chat_id: int | None = None,
    limit: int = 30,
) -> list[dict]:
    """Search messages across all chats or a specific chat."""
    from app.database import async_session_maker
    from app.crud import account as account_crud

    async with async_session_maker() as db:
        account = await account_crud.get_account(db, account_id)
        if account is None:
            raise ValueError("Account not found")

    client = await get_authorized_client(account)
    me = await client.get_me()
    my_user_id = me.id if me else None

    results = []
    async for msg in client.iter_messages(limit=limit, search=query):
        m = _message_to_dict(msg, my_user_id)
        if m:
            peer_id = _peer_id(msg.peer_id)
            try:
                entity = await client.get_entity(msg.peer_id)
                chat_title = entity.title if hasattr(entity, 'title') and entity.title else (
                    f"{entity.first_name or ''} {entity.last_name or ''}".strip() if isinstance(entity, User) else str(getattr(entity, 'id', ''))
                )
            except:
                chat_title = str(peer_id) if peer_id else "Unknown"
            m["chat_id"] = peer_id
            m["chat_title"] = chat_title
            results.append(m)

    return results
