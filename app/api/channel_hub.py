import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from telethon.tl.types import KeyboardButtonUrl, ReplyInlineMarkup

from app.api.deps import get_current_identity, Identity, require_account_tenant_access
from app.core.logging import get_logger
from app.crud import account as account_crud
from app.database import get_db
from app.schemas.channel_hub import ChannelHubPublishRequest, ChannelHubPublishResponse
from app.services.delivery import DeliveryStatus, classify_error
from app.services.telegram_actions import AccountNotAuthenticatedError, get_authorized_client

router = APIRouter(prefix="/api/channel-hub", tags=["channel-hub"])
logger = get_logger(__name__)

_STATUS_TO_HTTP = {
    DeliveryStatus.FLOOD_WAIT: status.HTTP_429_TOO_MANY_REQUESTS,
    DeliveryStatus.NETWORK_ERROR: status.HTTP_502_BAD_GATEWAY,
    DeliveryStatus.SESSION_EXPIRED: status.HTTP_400_BAD_REQUEST,
    DeliveryStatus.INVALID_RECIPIENT: status.HTTP_400_BAD_REQUEST,
    DeliveryStatus.FORBIDDEN: status.HTTP_403_FORBIDDEN,
    DeliveryStatus.BANNED: status.HTTP_403_FORBIDDEN,
    DeliveryStatus.PERMANENT_FAILURE: status.HTTP_502_BAD_GATEWAY,
    DeliveryStatus.INTERNAL_ERROR: status.HTTP_500_INTERNAL_SERVER_ERROR,
}


def _resolve_target(chat_id: str) -> int | str:
    stripped = chat_id.lstrip("-")
    return int(chat_id) if stripped.isdigit() else chat_id


@router.post("/publish", response_model=ChannelHubPublishResponse, status_code=status.HTTP_201_CREATED)
async def publish_channel_post(
    payload: ChannelHubPublishRequest,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Publish a single message (optionally with inline buttons, optionally
    pinned) to one Telegram chat/channel — a one-shot publish action, not a
    bulk broadcast, so it doesn't go through the Broadcast/rate-limit pipeline."""
    await require_account_tenant_access(payload.account_id, db, identity)
    account = await account_crud.get_account(db, payload.account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="계정을 찾을 수 없습니다.")

    try:
        client = await get_authorized_client(account)
    except AccountNotAuthenticatedError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    text = f"**{payload.title}**\n\n{payload.body}".strip() if payload.body else payload.title

    buttons = None
    if payload.buttons:
        rows = [[KeyboardButtonUrl(text=b.label, url=b.url)] for b in payload.buttons]
        buttons = ReplyInlineMarkup(rows=rows)

    target = _resolve_target(payload.channel_id)

    try:
        result = await client.send_message(target, text, buttons=buttons)
    except Exception as exc:
        delivery_status, safe_error = classify_error(exc)
        logger.warning(
            "channel_hub_publish_failed", account_id=account.id, channel_id=payload.channel_id,
            status=delivery_status.value, error=str(exc),
        )
        raise HTTPException(
            status_code=_STATUS_TO_HTTP.get(delivery_status, status.HTTP_502_BAD_GATEWAY),
            detail=safe_error or "발행에 실패했습니다.",
        )

    message_id = result.id if hasattr(result, "id") else None

    pinned = False
    if payload.pin_message and message_id is not None:
        try:
            await client.pin_message(target, message_id, notify=False)
            pinned = True
        except Exception as exc:
            logger.warning(
                "channel_hub_pin_failed", account_id=account.id, channel_id=payload.channel_id, error=str(exc),
            )

    logger.info("channel_hub_published", account_id=account.id, channel_id=payload.channel_id, message_id=message_id, pinned=pinned)
    return ChannelHubPublishResponse(
        id=str(uuid.uuid4()),
        message_id=message_id,
        published_at=datetime.now(timezone.utc).isoformat(),
        pinned=pinned,
    )
