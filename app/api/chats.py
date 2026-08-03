"""Telegram Chat API — Nicegram-style chat in dashboard."""

import asyncio
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_identity, Identity, require_account_tenant_access
from app.core.crypto import decrypt_session
from app.core.logging import get_logger
from app.crud import account as account_crud
from app.database import get_db
from app.models.bookmark import Bookmark
from app.schemas.chat_telegram import (
    TelegramDialog, TelegramMessage, SendMessageRequest,
    SendMessageResponse, ChatListResponse,
    EditMessageRequest, ForwardMessageRequest, ReactRequest,
)
from app.services.chat_actions import (
    list_dialogs, fetch_messages, send_chat_message, stream_new_messages,
    search_messages, send_typing_indicator, mute_dialog, pin_dialog, delete_dialog,
)
from app.services.media import save_broadcast_media, infer_media_type
from app.services.telethon_pool import pool
from app.services.telegram_actions import AccountNotAuthenticatedError
from telethon.errors import FloodWaitError

router = APIRouter(prefix="/api/chat-telegram", tags=["chat-telegram"])
logger = get_logger(__name__)

_WS_BROADCAST: list = []
_NOTIFICATION_SETTINGS: dict[str, dict] = {}


def broadcast_to_ws(event_type: str, data: dict):
    """Queue an event for WebSocket broadcast."""
    _WS_BROADCAST.append({"type": event_type, "data": data, "timestamp": datetime.utcnow().isoformat()})
    if len(_WS_BROADCAST) > 100:
        _WS_BROADCAST.pop(0)


def pop_ws_events() -> list[dict]:
    """Pop and return all pending broadcast events."""
    events = _WS_BROADCAST.copy()
    _WS_BROADCAST.clear()
    return events

# In-memory WebSocket clients per account: account_id → set of WebSocket
chat_ws_clients: dict[str, set[WebSocket]] = {}


@router.websocket("/ws")
async def chat_websocket(
    websocket: WebSocket,
    account_id: str = Query(...),
):
    await websocket.accept()
    if account_id not in chat_ws_clients:
        chat_ws_clients[account_id] = set()
    chat_ws_clients[account_id].add(websocket)

    async def send_broadcast_events():
        while True:
            try:
                events = pop_ws_events()
                for event in events:
                    dead: list[WebSocket] = []
                    for client in chat_ws_clients.get(account_id, set()):
                        try:
                            await client.send_json(event)
                        except WebSocketDisconnect:
                            dead.append(client)
                    for d in dead:
                        chat_ws_clients[account_id].discard(d)
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                break

    broadcast_task = asyncio.create_task(send_broadcast_events())
    try:
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)
            if msg.get("type") == "typing":
                for client in chat_ws_clients.get(account_id, set()):
                    if client != websocket:
                        try:
                            await client.send_json(msg)
                        except WebSocketDisconnect:
                            chat_ws_clients[account_id].discard(client)
    except WebSocketDisconnect:
        chat_ws_clients[account_id].discard(websocket)
        if not chat_ws_clients[account_id]:
            del chat_ws_clients[account_id]
    finally:
        broadcast_task.cancel()
        try:
            await broadcast_task
        except asyncio.CancelledError:
            pass


async def broadcast_dialog_update(account_id: str):
    """Push a dialog_update event to all connected clients for an account."""
    if account_id not in chat_ws_clients:
        return
    payload = json.dumps({"type": "dialog_update"})
    dead: list[WebSocket] = []
    for client in chat_ws_clients[account_id]:
        try:
            await client.send_text(payload)
        except WebSocketDisconnect:
            dead.append(client)
    for client in dead:
        chat_ws_clients[account_id].discard(client)
    if not chat_ws_clients[account_id]:
        del chat_ws_clients[account_id]


@router.get("/accounts/{account_id}/dialogs", response_model=list[TelegramDialog])
async def get_dialogs(
    account_id: str,
    limit: int = Query(100, ge=1, le=500),
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    """List all Telegram dialogs for an account (1:1, groups, channels)."""
    account = await account_crud.get_account(db, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    await require_account_tenant_access(account_id, db, identity)

    try:
        dialogs = await list_dialogs(account_id, limit=limit)
    except AccountNotAuthenticatedError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except FloodWaitError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Telegram rate limit exceeded. Retry after {exc.seconds} seconds.",
        )
    except Exception as exc:
        logger.exception(f"Unexpected error fetching dialogs for {account_id}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Telegram API 오류가 발생했습니다.")

    return dialogs


@router.get("/accounts/{account_id}/dialogs/{chat_id}/messages", response_model=list[TelegramMessage])
async def get_messages_endpoint(
    account_id: str,
    chat_id: int = Path(..., description="Telegram chat ID"),
    limit: int = Query(50, ge=1, le=200),
    offset_id: int | None = Query(None),
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    """Fetch messages from a specific Telegram chat."""
    account = await account_crud.get_account(db, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    await require_account_tenant_access(account_id, db, identity)

    messages = await fetch_messages(account_id, chat_id, limit=limit, offset_id=offset_id)
    return messages


@router.post("/accounts/{account_id}/upload")
async def upload_attachment_endpoint(
    account_id: str,
    file: UploadFile = File(...),
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    account = await account_crud.get_account(db, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    await require_account_tenant_access(account_id, db, identity)

    path = await save_broadcast_media(file)
    media_type = infer_media_type(file.filename or "")
    return {
        "success": True,
        "media_path": path,
        "media_type": media_type,
        "file_name": file.filename or "upload",
    }


@router.post("/accounts/{account_id}/dialogs/{chat_id}/send", response_model=SendMessageResponse)
async def send_message_endpoint(
    account_id: str,
    chat_id: int,
    body: SendMessageRequest,
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    """Send a message to a Telegram chat."""
    account = await account_crud.get_account(db, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    await require_account_tenant_access(account_id, db, identity)

    result = await send_chat_message(
        account_id, chat_id, body.text,
        reply_to_msg_id=body.reply_to_msg_id,
        media_path=body.media_path,
        media_type=body.media_type,
    )
    return result


async def _get_account_or_404(account_id: str, db: AsyncSession):
    account = await account_crud.get_account(db, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return account


def _config_error_to_http(exc: RuntimeError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))


@router.put("/accounts/{account_id}/dialogs/{chat_id}/messages/{message_id}")
async def edit_message(
    account_id: str,
    chat_id: str,
    message_id: int,
    payload: EditMessageRequest,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    await require_account_tenant_access(account_id, db, identity)
    account = await _get_account_or_404(account_id, db)

    try:
        client = await pool.get_client(account.id, decrypt_session(account.session_data) if account.session_data else "")
    except RuntimeError as exc:
        raise _config_error_to_http(exc)

    from app.services.chat_actions import edit_chat_message
    try:
        result = await edit_chat_message(client, int(chat_id), message_id, payload.text)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except FloodWaitError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Telegram rate limit exceeded. Retry after {exc.seconds} seconds.",
        )
    broadcast_to_ws("message_edited", {"chat_id": chat_id, "message_id": message_id, "text": payload.text})
    return result


@router.delete("/accounts/{account_id}/dialogs/{chat_id}/messages/{message_id}")
async def delete_message(
    account_id: str,
    chat_id: str,
    message_id: int,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
    revoke: bool = False,
):
    await require_account_tenant_access(account_id, db, identity)
    account = await _get_account_or_404(account_id, db)

    try:
        client = await pool.get_client(account.id, decrypt_session(account.session_data) if account.session_data else "")
    except RuntimeError as exc:
        raise _config_error_to_http(exc)

    from app.services.chat_actions import delete_chat_message
    try:
        await delete_chat_message(client, int(chat_id), message_id, revoke=revoke)
    except FloodWaitError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Telegram rate limit exceeded. Retry after {exc.seconds} seconds.",
        )
    broadcast_to_ws("message_deleted", {"chat_id": chat_id, "message_id": message_id})
    return {"ok": True}


@router.post("/accounts/{account_id}/dialogs/{chat_id}/messages/{message_id}/forward")
async def forward_message(
    account_id: str,
    chat_id: str,
    message_id: int,
    payload: ForwardMessageRequest,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    await require_account_tenant_access(account_id, db, identity)
    account = await _get_account_or_404(account_id, db)

    try:
        client = await pool.get_client(account.id, decrypt_session(account.session_data) if account.session_data else "")
    except RuntimeError as exc:
        raise _config_error_to_http(exc)

    from app.services.chat_actions import forward_chat_message
    try:
        result = await forward_chat_message(client, int(chat_id), message_id, int(payload.target_chat_id))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except FloodWaitError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Telegram rate limit exceeded. Retry after {exc.seconds} seconds.",
        )
    return result


@router.post("/accounts/{account_id}/dialogs/{chat_id}/messages/{message_id}/react")
async def send_reaction(
    account_id: str,
    chat_id: str,
    message_id: int,
    payload: ReactRequest,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    await require_account_tenant_access(account_id, db, identity)
    account = await _get_account_or_404(account_id, db)

    try:
        client = await pool.get_client(account.id, decrypt_session(account.session_data) if account.session_data else "")
    except RuntimeError as exc:
        raise _config_error_to_http(exc)

    from app.services.chat_actions import send_message_reaction
    try:
        await send_message_reaction(client, int(chat_id), message_id, payload.emoji)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except FloodWaitError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Telegram rate limit exceeded. Retry after {exc.seconds} seconds.",
        )
    return {"ok": True}


@router.get("/accounts/{account_id}/dialogs/{chat_id}/stream")
async def stream_messages_endpoint(
    account_id: str,
    chat_id: int,
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    """SSE stream for real-time new messages."""
    account = await account_crud.get_account(db, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    await require_account_tenant_access(account_id, db, identity)

    return StreamingResponse(
        stream_new_messages(account_id, chat_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/accounts/{account_id}/search")
async def search_messages_endpoint(
    account_id: str,
    q: str = Query(..., min_length=1, max_length=200, description="검색어"),
    chat_id: int | None = Query(None),
    limit: int = Query(30, ge=1, le=100),
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    account = await account_crud.get_account(db, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    await require_account_tenant_access(account_id, db, identity)
    results = await search_messages(account_id, q, chat_id=chat_id, limit=limit)
    return results


@router.post("/accounts/{account_id}/dialogs/{chat_id}/typing")
async def typing_indicator_endpoint(
    account_id: str,
    chat_id: int,
    body: dict,
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    account = await account_crud.get_account(db, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    await require_account_tenant_access(account_id, db, identity)
    typing = body.get("typing", True)
    await send_typing_indicator(account_id, chat_id, typing=typing)
    return {"status": "ok"}


@router.post("/accounts/{account_id}/dialogs/{chat_id}/mute")
async def mute_dialog_endpoint(
    account_id: str,
    chat_id: int,
    body: dict,
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    account = await account_crud.get_account(db, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    await require_account_tenant_access(account_id, db, identity)
    result = await mute_dialog(account_id, chat_id, mute=body.get("mute", True))
    return result


@router.post("/accounts/{account_id}/dialogs/{chat_id}/pin")
async def pin_dialog_endpoint(
    account_id: str,
    chat_id: int,
    body: dict,
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    account = await account_crud.get_account(db, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    await require_account_tenant_access(account_id, db, identity)
    result = await pin_dialog(account_id, chat_id, pin=body.get("pin", True))
    return result


@router.delete("/accounts/{account_id}/dialogs/{chat_id}")
async def delete_dialog_endpoint(
    account_id: str,
    chat_id: int,
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    account = await account_crud.get_account(db, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    await require_account_tenant_access(account_id, db, identity)
    result = await delete_dialog(account_id, chat_id)
    return result


@router.get("/accounts/{account_id}/dialogs/{chat_id}/info")
async def get_chat_info(
    account_id: str,
    chat_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Get detailed info about a chat: name, photo, members, etc."""
    await require_account_tenant_access(account_id, db, identity)
    account = await _get_account_or_404(account_id, db)
    try:
        client = await pool.get_client(account.id, decrypt_session(account.session_data) if account.session_data else "")
    except RuntimeError as exc:
        raise _config_error_to_http(exc)

    from app.services.chat_actions import get_chat_details
    try:
        return await get_chat_details(client, int(chat_id))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


class CreateGroupRequest(BaseModel):
    title: str
    user_ids: list[str] = []


@router.post("/accounts/{account_id}/groups/create")
async def create_group(
    account_id: str,
    payload: CreateGroupRequest,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    await require_account_tenant_access(account_id, db, identity)
    account = await _get_account_or_404(account_id, db)
    try:
        client = await pool.get_client(account.id, decrypt_session(account.session_data) if account.session_data else "")
    except RuntimeError as exc:
        raise _config_error_to_http(exc)

    from app.services.chat_actions import create_telegram_group
    try:
        return await create_telegram_group(client, payload.title, payload.user_ids)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


class NotificationSettingsRequest(BaseModel):
    muted: bool = False
    sound: str = "default"


@router.post("/accounts/{account_id}/dialogs/{chat_id}/notifications")
async def set_notification_settings(
    account_id: str,
    chat_id: str,
    payload: NotificationSettingsRequest,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    await require_account_tenant_access(account_id, db, identity)
    key = f"{account_id}:{chat_id}"
    _NOTIFICATION_SETTINGS[key] = {"muted": payload.muted, "sound": payload.sound}
    return {"ok": True}


@router.get("/accounts/{account_id}/dialogs/{chat_id}/notifications")
async def get_notification_settings(
    account_id: str,
    chat_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    await require_account_tenant_access(account_id, db, identity)
    key = f"{account_id}:{chat_id}"
    return _NOTIFICATION_SETTINGS.get(key, {"muted": False, "sound": "default"})


@router.get("/accounts/{account_id}/dialogs/{chat_id}/export")
async def export_chat(
    account_id: str,
    chat_id: str,
    format: str = "json",
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Export chat history as JSON or text."""
    await require_account_tenant_access(account_id, db, identity)
    account = await _get_account_or_404(account_id, db)
    try:
        client = await pool.get_client(account.id, decrypt_session(account.session_data) if account.session_data else "")
    except RuntimeError as exc:
        raise _config_error_to_http(exc)

    from app.services.chat_actions import export_chat_history
    try:
        return await export_chat_history(client, int(chat_id), format)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.post("/accounts/{account_id}/dialogs/{chat_id}/send-sticker")
async def send_sticker(
    account_id: str,
    chat_id: str,
    payload: dict,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    await require_account_tenant_access(account_id, db, identity)
    account = await _get_account_or_404(account_id, db)
    try:
        client = await pool.get_client(account.id, decrypt_session(account.session_data) if account.session_data else "")
    except RuntimeError as exc:
        raise _config_error_to_http(exc)

    from app.services.chat_actions import send_chat_sticker
    try:
        result = await send_chat_sticker(client, int(chat_id), payload.get("sticker_id", ""), payload.get("emoji", ""))
    except FloodWaitError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Telegram rate limit exceeded. Retry after {exc.seconds} seconds.",
        )
    return result


@router.get("/accounts/{account_id}/stickers")
async def search_stickers_endpoint(
    account_id: str,
    query: str = "",
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    await require_account_tenant_access(account_id, db, identity)
    account = await _get_account_or_404(account_id, db)
    try:
        client = await pool.get_client(account.id, decrypt_session(account.session_data) if account.session_data else "")
    except RuntimeError as exc:
        raise _config_error_to_http(exc)

    from app.services.chat_actions import search_stickers
    return await search_stickers(client, query)


@router.get("/accounts/{account_id}/users/{user_id}/profile")
async def get_user_profile(
    account_id: str,
    user_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    await require_account_tenant_access(account_id, db, identity)
    account = await _get_account_or_404(account_id, db)
    try:
        client = await pool.get_client(account.id, decrypt_session(account.session_data) if account.session_data else "")
    except RuntimeError as exc:
        raise _config_error_to_http(exc)

    from app.services.chat_actions import get_user_profile_info
    return await get_user_profile_info(client, int(user_id))


_BOOKMARKS_TABLE_ENSURED = False


async def _ensure_bookmarks_table(db: AsyncSession) -> None:
    """Create the bookmarks table if it does not exist."""
    global _BOOKMARKS_TABLE_ENSURED
    if _BOOKMARKS_TABLE_ENSURED:
        return
    await db.execute(text("""
        CREATE TABLE IF NOT EXISTS bookmarks (
            id SERIAL PRIMARY KEY,
            tenant_id VARCHAR(36) NOT NULL,
            account_id VARCHAR(36) NOT NULL,
            chat_id VARCHAR(36) NOT NULL,
            message_id INTEGER NOT NULL,
            text VARCHAR(4000),
            sender_name VARCHAR(200),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """))
    await db.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_bookmarks_tenant_id
        ON bookmarks (tenant_id)
    """))
    await db.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_bookmarks_account_id
        ON bookmarks (account_id)
    """))
    await db.commit()
    _BOOKMARKS_TABLE_ENSURED = True


@router.get("/bookmarks")
async def get_bookmarks(
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_bookmarks_table(db)
    key = identity.tenant_id or identity.user_id or "default"
    from sqlalchemy import select
    result = await db.execute(
        select(Bookmark).where(Bookmark.tenant_id == key).order_by(Bookmark.created_at.desc())
    )
    bookmarks = result.scalars().all()
    return [
        {
            "id": b.message_id,
            "chat_id": b.chat_id,
            "text": b.text,
            "sender_name": b.sender_name,
            "saved_at": b.created_at.isoformat() if b.created_at else None,
        }
        for b in bookmarks
    ]


@router.post("/bookmarks")
async def add_bookmark(
    body: dict,
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_bookmarks_table(db)
    key = identity.tenant_id or identity.user_id or "default"
    bookmark = Bookmark(
        tenant_id=key,
        account_id=body.get("account_id", ""),
        chat_id=str(body.get("chat_id", "")),
        message_id=body.get("message_id", 0),
        text=body.get("text", "")[:200],
        sender_name=body.get("sender_name", ""),
    )
    db.add(bookmark)
    await db.commit()
    return {"status": "saved"}


@router.delete("/bookmarks/{message_id}")
async def remove_bookmark(
    message_id: int,
    chat_id: int = Query(...),
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_bookmarks_table(db)
    key = identity.tenant_id or identity.user_id or "default"
    from sqlalchemy import delete as sa_delete
    await db.execute(
        sa_delete(Bookmark).where(
            Bookmark.tenant_id == key,
            Bookmark.message_id == message_id,
            Bookmark.chat_id == str(chat_id),
        )
    )
    await db.commit()
    return {"status": "removed"}


_CHAT_META_TABLE_ENSURED = False


async def _ensure_chat_meta_table(db: AsyncSession) -> None:
    """Create the chat_meta table if it does not exist (see _ensure_bookmarks_table:
    this app's alembic history has two disconnected chains and a DB stamped ahead of
    where its physical schema actually is, so migrations silently no-op on this deploy
    — self-healing DDL here is the reliable path until that gets untangled)."""
    global _CHAT_META_TABLE_ENSURED
    if _CHAT_META_TABLE_ENSURED:
        return
    await db.execute(text("""
        CREATE TABLE IF NOT EXISTS chat_meta (
            id VARCHAR(36) PRIMARY KEY,
            tenant_id VARCHAR(36) NOT NULL,
            account_id VARCHAR(36) NOT NULL,
            chat_id VARCHAR(36) NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'new',
            assignee_id VARCHAR(36),
            note TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_chat_meta_chat UNIQUE (tenant_id, account_id, chat_id)
        )
    """))
    await db.execute(text("CREATE INDEX IF NOT EXISTS ix_chat_meta_tenant_id ON chat_meta (tenant_id)"))
    await db.execute(text("CREATE INDEX IF NOT EXISTS ix_chat_meta_account_id ON chat_meta (account_id)"))
    await db.execute(text("CREATE INDEX IF NOT EXISTS ix_chat_meta_chat_id ON chat_meta (chat_id)"))
    await db.execute(text("CREATE INDEX IF NOT EXISTS ix_chat_meta_assignee_id ON chat_meta (assignee_id)"))
    await db.commit()
    _CHAT_META_TABLE_ENSURED = True


@router.get("/chat-meta")
async def list_chat_meta(
    account_id: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    assignee_id: str | None = Query(default=None),
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    """List CRM metadata for every chat that has any, optionally filtered — powers
    cross-account/cross-status filters in the chat list without a per-chat round trip."""
    await _ensure_chat_meta_table(db)
    from sqlalchemy import select
    from app.models.chat_meta import ChatMeta

    tenant_id = identity.tenant_id or "default"
    conditions = [ChatMeta.tenant_id == tenant_id]
    if account_id:
        conditions.append(ChatMeta.account_id == account_id)
    if status_filter:
        conditions.append(ChatMeta.status == status_filter)
    if assignee_id:
        conditions.append(ChatMeta.assignee_id == assignee_id)

    result = await db.execute(select(ChatMeta).where(*conditions))
    rows = result.scalars().all()
    return [
        {
            "account_id": r.account_id,
            "chat_id": r.chat_id,
            "status": r.status,
            "assignee_id": r.assignee_id,
            "note": r.note,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        }
        for r in rows
    ]


class ChatMetaUpdate(BaseModel):
    status: str | None = None
    assignee_id: str | None = None
    note: str | None = None


@router.put("/chat-meta/{account_id}/{chat_id}")
async def upsert_chat_meta(
    account_id: str,
    chat_id: str,
    body: ChatMetaUpdate,
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_chat_meta_table(db)
    await require_account_tenant_access(account_id, db, identity)
    from sqlalchemy import select
    from app.models.chat_meta import CHAT_STATUSES, ChatMeta
    import uuid

    if body.status is not None and body.status not in CHAT_STATUSES:
        raise HTTPException(status_code=400, detail=f"status must be one of {CHAT_STATUSES}")

    tenant_id = identity.tenant_id or "default"
    result = await db.execute(
        select(ChatMeta).where(
            ChatMeta.tenant_id == tenant_id,
            ChatMeta.account_id == account_id,
            ChatMeta.chat_id == chat_id,
        )
    )
    meta = result.scalar_one_or_none()
    if meta is None:
        meta = ChatMeta(id=str(uuid.uuid4()), tenant_id=tenant_id, account_id=account_id, chat_id=chat_id)
        db.add(meta)

    if body.status is not None:
        meta.status = body.status
    if body.assignee_id is not None:
        meta.assignee_id = body.assignee_id or None
    if body.note is not None:
        meta.note = body.note or None

    await db.commit()
    return {
        "account_id": meta.account_id,
        "chat_id": meta.chat_id,
        "status": meta.status,
        "assignee_id": meta.assignee_id,
        "note": meta.note,
    }
