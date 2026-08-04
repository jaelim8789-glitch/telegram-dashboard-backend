"""Telegram chat operations: list dialogs, fetch messages, send messages, live SSE."""

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, AsyncGenerator

from telethon import utils
from telethon.tl.types import MessageService, User
from telethon.tl.functions.contacts import BlockRequest, UnblockRequest

from app.core.logging import get_logger
from app.database import async_session_maker
from app.crud import account as account_crud
from telethon.errors import AuthKeyUnregisteredError, FloodWaitError
from app.services.telegram_actions import get_authorized_client, AccountNotAuthenticatedError
from app.services.telegram_mappers import dialog_to_dict as _dialog_to_dict, message_to_dict as _message_to_dict

logger = get_logger(__name__)


async def list_dialogs(account_id: str, limit: int = 100) -> list[dict]:
    """List all Telegram dialogs (1:1, groups, channels) for an account."""
    async with async_session_maker() as db:
        account = await account_crud.get_account(db, account_id)
        if account is None:
            raise ValueError("Account not found")
    client = await get_authorized_client(account)
    try:
        me = await client.get_me()
        my_user_id = me.id if me else None

        dialogs = []
        async for dialog in client.iter_dialogs(limit=limit):
            try:
                d = _dialog_to_dict(dialog)
                if d:
                    dialogs.append(d)
            except Exception:
                logger.warning("chat_actions_dialog_parse_failed", account_id=account_id, dialog_id=getattr(dialog, "id", "unknown"))
                continue
        return dialogs
    except AuthKeyUnregisteredError as exc:
        # Session connected fine but Telegram has since revoked it (remote
        # logout, "terminate this session" from another device, etc.) — only
        # surfaces once an actual RPC call runs, so get_authorized_client's
        # own SessionInvalidError guard never catches it.
        raise AccountNotAuthenticatedError(
            "텔레그램 세션이 만료되었습니다(다른 기기에서 로그아웃되었을 수 있음). 계정을 다시 인증해주세요."
        ) from exc


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
        kwargs["max_id"] = offset_id - 1

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
            try:
                reply_msg = await client.get_messages(chat_id, ids=reply_id)
                if reply_msg and not isinstance(reply_msg, MessageService):
                    reply_map[reply_id] = getattr(reply_msg, "message", "") or ""
            except Exception:
                logger.warning("chat_actions_reply_fetch_failed", account_id=account_id, chat_id=chat_id, reply_id=reply_id)
                continue
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
    media_type: str | None = None,
) -> dict:
    """Send a message to a Telegram chat."""
    async with async_session_maker() as db:
        account = await account_crud.get_account(db, account_id)
        if account is None:
            raise ValueError("Account not found")
    client = await get_authorized_client(account)

    kwargs = {"comment_to": chat_id} if chat_id < 0 and abs(chat_id) > 10**12 else {}
    try:
        if media_path:
            sent = await client.send_file(
                chat_id,
                media_path,
                caption=text,
                reply_to=reply_to_msg_id or None,
                file_type=media_type,
            )
        else:
            sent = await client.send_message(chat_id, text, reply_to=reply_to_msg_id or None)
    except FloodWaitError as exc:
        raise ValueError(f"텔레그램 쓰로틀링: {exc.seconds}초 후 다시 시도해주세요.")
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
        # Telethon only caches an entity once it's seen it via get_dialogs()/messages —
        # a freshly-authenticated session hasn't cached every private-chat peer yet, so
        # get_messages(chat_id, ...) below can raise "Could not find the input entity"
        # for a chat_id it has genuinely never resolved. Warm the cache once up front.
        await client.get_dialogs(limit=50)
    except Exception as e:
        yield f"event: error\ndata: {str(e)}\n\n"
        return

    # Prime last_id from whatever already exists instead of starting at 0 --
    # otherwise every fresh connection (e.g. opening a room the client already
    # has via the initial REST fetch) replays the last 5 messages as if they
    # were brand new, right before the real polling loop starts below.
    try:
        initial_messages = await client.get_messages(chat_id, limit=5)
        last_id = max((m.id for m in initial_messages), default=0)
    except Exception:
        last_id = 0

    while True:
        try:
            messages = await client.get_messages(chat_id, limit=5)
            for msg in reversed(messages):
                m = _message_to_dict(msg, my_user_id)
                if m and m["id"] > last_id:
                    yield f"event: message\ndata: {json.dumps(m)}\n\n"
                    last_id = m["id"]
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            break
        except ValueError as e:
            # "Could not find the input entity" — the up-front get_dialogs() warm-up
            # can still miss an entity that's dropped out of the session's in-memory
            # cache on a long-lived stream. Re-resolve once and keep going instead of
            # logging + surfacing the same error to the client every 5s forever.
            try:
                await client.get_entity(chat_id)
            except Exception:
                logger.warning("chat_stream_error", chat_id=chat_id, error=str(e))
                yield f"event: error\ndata: {str(e)}\n\n"
                await asyncio.sleep(5)
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
    try:
        me = await client.get_me()
    except FloodWaitError as exc:
        raise ValueError(f"텔레그램 쓰로틀링: {exc.seconds}초 후 다시 시도해주세요.")
    my_user_id = me.id if me else None

    results = []
    try:
        async for msg in client.iter_messages(limit=limit, search=query):
            try:
                m = _message_to_dict(msg, my_user_id)
                if m:
                    peer_id = _peer_id(msg.peer_id)
                    try:
                        entity = await client.get_entity(msg.peer_id)
                        chat_title = entity.title if hasattr(entity, 'title') and entity.title else (
                            f"{entity.first_name or ''} {entity.last_name or ''}".strip() if isinstance(entity, User) else str(getattr(entity, 'id', ''))
                        )
                    except Exception:
                        chat_title = str(peer_id) if peer_id else "Unknown"
                    m["chat_id"] = peer_id
                    m["chat_title"] = chat_title
                    results.append(m)
            except Exception:
                logger.warning("chat_actions_search_message_failed", account_id=account_id, msg_id=getattr(msg, "id", "unknown"))
                continue
    except FloodWaitError as exc:
        raise ValueError(f"텔레그램 쓰로틀링: {exc.seconds}초 후 다시 시도해주세요.")

    return results


async def edit_chat_message(client, chat_id: int, message_id: int, text: str) -> dict:
    """Edit an existing message."""
    entity = await client.get_entity(chat_id)
    msg = await client.get_messages(entity, ids=message_id)
    if msg is None:
        raise ValueError("Message not found")
    edited = await client.edit_message(entity, msg, text)
    return {"ok": True, "message_id": edited.id}


async def delete_chat_message(client, chat_id: int, message_id: int, revoke: bool = False) -> None:
    """Delete a message."""
    entity = await client.get_entity(chat_id)
    await client.delete_messages(entity, [message_id], revoke=revoke)


async def forward_chat_message(client, chat_id: int, message_id: int, target_chat_id: int) -> dict:
    """Forward a message to another chat."""
    entity = await client.get_entity(chat_id)
    target = await client.get_entity(target_chat_id)
    result = await client.forward_messages(target, message_id, entity)
    return {"ok": True, "count": len(result) if result else 0}


async def send_message_reaction(client, chat_id: int, message_id: int, emoji: str) -> None:
    """Send a reaction to a message."""
    entity = await client.get_entity(chat_id)
    msg = await client.get_messages(entity, ids=message_id)
    if msg is None:
        raise ValueError("Message not found")
    await client.send_reaction(entity, msg, emoji)


async def pin_chat_message(client, chat_id: int, message_id: int, notify: bool = False) -> None:
    """Pin an individual message within a chat."""
    entity = await client.get_entity(chat_id)
    msg = await client.get_messages(entity, ids=message_id)
    if msg is None:
        raise ValueError("Message not found")
    await client.pin_message(entity, msg, notify=notify)


async def unpin_chat_message(client, chat_id: int, message_id: int) -> None:
    """Unpin an individual message within a chat."""
    entity = await client.get_entity(chat_id)
    msg = await client.get_messages(entity, ids=message_id)
    if msg is None:
        raise ValueError("Message not found")
    await client.unpin_message(entity, msg)


async def block_user(client, chat_id: int) -> None:
    """Block a user (1:1 chat peer)."""
    entity = await client.get_entity(chat_id)
    # BlockRequest.id is typed TypeInputPeer, not a full User — passing the
    # entity directly serializes the WRONG bytes on the wire (verified: the
    # entity's own TL constructor gets serialized in place of an InputPeer,
    # producing a malformed request Telegram would reject/misinterpret).
    # utils.get_input_peer() does the required User -> InputPeerUser (etc.)
    # conversion, same as Telethon's own convenience methods do internally.
    await client(BlockRequest(id=utils.get_input_peer(entity)))


async def unblock_user(client, chat_id: int) -> None:
    """Unblock a user (1:1 chat peer)."""
    entity = await client.get_entity(chat_id)
    await client(UnblockRequest(id=utils.get_input_peer(entity)))


async def get_chat_details(client, chat_id: int, account_id: str | None = None) -> dict:
    """Get detailed chat info including members.

    When account_id is provided, also downloads (and caches on disk, per-account/
    per-chat) the peer's profile photo and returns a servable photo_url -- see
    app.services.media.{avatar_file_path,avatar_is_fresh,save_avatar_bytes}.
    """
    entity = await client.get_entity(chat_id)
    info = {
        "id": chat_id,
        "title": getattr(entity, "title", None) or getattr(entity, "first_name", None) or "Unknown",
        "type": type(entity).__name__,
    }

    if account_id is not None:
        from app.services.media import (
            avatar_file_path, avatar_is_fresh, avatar_url_path, save_avatar_bytes,
        )
        cache_path = avatar_file_path(account_id, chat_id)
        try:
            if avatar_is_fresh(cache_path):
                info["has_photo"] = True
                info["photo_url"] = avatar_url_path(account_id, chat_id)
            else:
                photo = await client.download_profile_photo(entity, file=bytes)
                if photo:
                    save_avatar_bytes(account_id, chat_id, photo)
                    info["has_photo"] = True
                    info["photo_url"] = avatar_url_path(account_id, chat_id)
                else:
                    info["has_photo"] = False
        except Exception:
            logger.warning("chat_details_avatar_failed", account_id=account_id, chat_id=chat_id)
            # Fall back to a stale cached copy (if any) rather than surfacing no photo
            # at all just because this particular refresh attempt errored.
            if cache_path.exists():
                info["has_photo"] = True
                info["photo_url"] = avatar_url_path(account_id, chat_id)
            else:
                info["has_photo"] = False
    else:
        try:
            photo = await client.download_profile_photo(entity, file=bytes)
            if photo:
                info["has_photo"] = True
        except Exception:
            info["has_photo"] = False

    if hasattr(entity, "participants_count"):
        info["members_count"] = entity.participants_count

    if hasattr(entity, "phone"):
        info["phone"] = entity.phone
    if hasattr(entity, "username"):
        info["username"] = entity.username
    if hasattr(entity, "status"):
        status_name = type(entity.status).__name__
        info["online_status"] = status_name

    return info


async def create_telegram_group(client, title: str, user_ids: list[str]) -> dict:
    """Create a new Telegram group."""
    users = []
    for uid in user_ids:
        try:
            entity = await client.get_entity(uid)
            users.append(entity)
        except Exception:
            continue

    if not users:
        raise ValueError("No valid users found")

    chat = await client.create_group(title, users)
    return {"ok": True, "chat_id": chat.id, "title": title}


async def export_chat_history(client, chat_id: int, format: str = "json", limit: int = 500) -> list | dict:
    """Export recent chat history."""
    entity = await client.get_entity(chat_id)
    messages = await client.get_messages(entity, limit=limit)

    result = []
    for msg in reversed(messages):
        if msg is None:
            continue
        try:
            entry = {
                "id": msg.id,
                "date": msg.date.isoformat() if msg.date else None,
                "sender_id": msg.sender_id,
                "text": msg.text or "",
                "is_outgoing": msg.out,
            }
            if format == "text":
                sender = "You" if msg.out else "Other"
                entry = f"[{entry['date']}] {sender}: {entry['text']}"
            result.append(entry)
        except Exception:
            logger.warning("chat_actions_export_message_failed", chat_id=chat_id, msg_id=getattr(msg, "id", "unknown"))
            continue

    if format == "text":
        return {"content": "\n".join(result), "count": len(result)}
    return {"messages": result, "count": len(result)}


async def send_chat_sticker(client, chat_id: int, sticker_id: str, emoji: str = "") -> dict:
    """Send a sticker to a chat."""
    entity = await client.get_entity(chat_id)
    msg = await client.send_file(entity, sticker_id, force_document=True)
    return {"ok": True, "message_id": msg.id}


async def search_stickers(client, query: str) -> list:
    """Search for stickers."""
    try:
        result = await client.get_stickers(query)
        stickers = []
        for sticker in result[:20]:
            stickers.append({
                "id": sticker.id,
                "emoji": getattr(sticker, "emoji", ""),
                "file_size": getattr(sticker, "size", 0),
                "document_id": getattr(sticker, "id", 0),
            })
        return stickers
    except Exception:
        return []


async def get_user_profile_info(client, user_id: int) -> dict:
    """Get detailed user profile info."""
    try:
        user = await client.get_entity(user_id)
        profile = {
            "id": user_id,
            "first_name": getattr(user, "first_name", ""),
            "last_name": getattr(user, "last_name", ""),
            "username": getattr(user, "username", ""),
            "phone": getattr(user, "phone", None),
            "is_self": getattr(user, "self", False),
            "is_bot": getattr(user, "bot", False),
        }

        status = getattr(user, "status", None)
        if status:
            profile["online_status"] = type(status).__name__

        try:
            photos = []
            async for photo in client.iter_profile_photos(user, limit=5):
                photos.append(photo.id)
            profile["recent_photos_count"] = len(photos)
        except Exception:
            profile["recent_photos_count"] = 0

        return profile
    except Exception as e:
        return {"id": user_id, "error": str(e)}
