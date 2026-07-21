"""Telegram Chat API — Nicegram-style chat in dashboard."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_identity, Identity, require_account_tenant_access
from app.core.logging import get_logger
from app.database import get_db
from app.schemas.chat_telegram import (
    TelegramDialog, TelegramMessage, SendMessageRequest,
    SendMessageResponse, ChatListResponse,
)
from app.services.chat_actions import (
    list_dialogs, fetch_messages, send_chat_message, stream_new_messages,
    search_messages, send_typing_indicator, mute_dialog, pin_dialog, delete_dialog,
)
from app.crud import account as account_crud

router = APIRouter(prefix="/api/chat-telegram", tags=["chat-telegram"])
logger = get_logger(__name__)


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
    require_account_tenant_access(identity, account)

    dialogs = await list_dialogs(account_id, limit=limit)
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
    require_account_tenant_access(identity, account)

    messages = await fetch_messages(account_id, chat_id, limit=limit, offset_id=offset_id)
    return messages


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
    require_account_tenant_access(identity, account)

    result = await send_chat_message(
        account_id, chat_id, body.text,
        reply_to_msg_id=body.reply_to_msg_id,
        media_path=body.media_path,
    )
    return result


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
    require_account_tenant_access(identity, account)

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
    require_account_tenant_access(identity, account)
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
    require_account_tenant_access(identity, account)
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
    require_account_tenant_access(identity, account)
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
    require_account_tenant_access(identity, account)
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
    require_account_tenant_access(identity, account)
    result = await delete_dialog(account_id, chat_id)
    return result


_BOOKMARKS: dict[str, list[dict]] = {}


@router.get("/bookmarks")
async def get_bookmarks(
    identity: Identity = Depends(get_current_identity),
):
    key = identity.tenant_id or identity.user_id or "default"
    return _BOOKMARKS.get(key, [])


@router.post("/bookmarks")
async def add_bookmark(
    body: dict,
    identity: Identity = Depends(get_current_identity),
):
    key = identity.tenant_id or identity.user_id or "default"
    if key not in _BOOKMARKS:
        _BOOKMARKS[key] = []
    _BOOKMARKS[key].append({
        "id": body.get("message_id"),
        "chat_id": body.get("chat_id"),
        "chat_title": body.get("chat_title", ""),
        "text": body.get("text", "")[:200],
        "sender_name": body.get("sender_name", ""),
        "date": body.get("date"),
        "saved_at": str(datetime.now(timezone.utc)),
    })
    return {"status": "saved"}


@router.delete("/bookmarks/{message_id}")
async def remove_bookmark(
    message_id: int,
    chat_id: int = Query(...),
    identity: Identity = Depends(get_current_identity),
):
    key = identity.tenant_id or identity.user_id or "default"
    items = _BOOKMARKS.get(key, [])
    _BOOKMARKS[key] = [b for b in items if not (b["id"] == message_id and b["chat_id"] == chat_id)]
    return {"status": "removed"}
