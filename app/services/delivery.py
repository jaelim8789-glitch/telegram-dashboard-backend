"""
Canonical Telegram message delivery pipeline.

Owns:
1. Account resolution + DB lookup
2. Session/client restoration
3. Recipient resolution
4. Send attempt with Telethon
5. Telegram response capture
6. MessageLog persistence
7. Failure classification
8. Retry decision
9. Operational event publication via callback

All Telegram send paths (broadcast, reply macro, auto reply, scheduled)
should route through this pipeline.
"""

import asyncio
import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from telethon import TelegramClient
from telethon.errors import (
    ChatAdminRequiredError,
    ChatWriteForbiddenError,
    FloodWaitError,
    InputUserDeactivatedError,
    PhoneNumberBannedError,
    RPCError,
    SlowModeWaitError,
    UserBannedInChannelError,
    UserDeactivatedBanError,
    UserIsBlockedError,
    UserKickedError,
    UserNotParticipantError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)
from telethon.tl.types import ReplyInlineMarkup, KeyboardButtonUrl

from app.core.logging import get_logger
from app.crud import account as account_crud
from app.database import async_session_maker
from app.models.account import Account
from app.models.message_log import MessageLog
from app.services.telegram_actions import AccountNotAuthenticatedError, get_authorized_client

logger = get_logger(__name__)

# ─── Failure Taxonomy ─────────────────────────────────────────────────


class DeliveryStatus(str, enum.Enum):
    SUCCESS = "success"
    FLOOD_WAIT = "flood_wait"
    NETWORK_ERROR = "network_error"
    SESSION_EXPIRED = "session_expired"
    INVALID_RECIPIENT = "invalid_recipient"
    FORBIDDEN = "forbidden"
    BANNED = "banned"
    PERMANENT_FAILURE = "permanent_failure"
    INTERNAL_ERROR = "internal_error"


RECOVERABLE_STATUSES = {
    DeliveryStatus.FLOOD_WAIT,
    DeliveryStatus.NETWORK_ERROR,
}

MAX_RETRIES = 3
BASE_BACKOFF_SECONDS = 5.0
MAX_WAIT_SECONDS = 60.0

# Per-recipient send timeout. Without this, a single Telethon call that hangs
# (dead connection, DC migration stall, etc.) silently consumes the *entire*
# broadcast-level timeout budget — the outer asyncio.wait_for in
# broadcast_processor.py then cancels the whole broadcast, even though every
# other recipient would have gone through fine. Bounding each individual send
# turns a full-broadcast stall into one classified, retriable failure.
PER_MESSAGE_TIMEOUT_SECONDS = 30.0

EVENT_QUEUED = "queued"
EVENT_SENDING = "sending"
EVENT_RETRYING = "retrying"
EVENT_SENT = "sent"
EVENT_FAILED = "failed"


@dataclass
class DeliveryResult:
    status: DeliveryStatus
    recipient: str
    telegram_message_id: int | None = None
    error_message: str | None = None
    flood_wait_seconds: int | None = None
    attempt_count: int = 1


@dataclass
class DeliveryRequest:
    account_id: str
    recipients: list[str]
    message: str
    media_path: str | None = None
    source: str = "manual"
    source_id: str | None = None
    on_status_change: Callable | None = None
    inter_message_delay: float = 1.0
    reply_to_msg_id: int | None = None
    reply_to_map: dict[str, int] | None = None
    inline_buttons: list[dict] | None = None


def utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def classify_error(exc: Exception) -> tuple[DeliveryStatus, str | None]:
    """Classify a Telethon exception into a DeliveryStatus.

    Returns (status, safe_error_message).
    Never exposes raw exception details, session data, or secrets to clients.
    """
    if isinstance(exc, FloodWaitError):
        return DeliveryStatus.FLOOD_WAIT, f"텔레그램 속도 제한: {exc.seconds}초 대기 필요"

    if isinstance(exc, (UserDeactivatedBanError, PhoneNumberBannedError)):
        return DeliveryStatus.BANNED, "계정이 텔레그램에서 차단되었습니다."

    if isinstance(exc, (UserIsBlockedError, ChatWriteForbiddenError,
                        ChatAdminRequiredError, UserBannedInChannelError, UserKickedError)):
        return DeliveryStatus.FORBIDDEN, "해당 채팅방에 메시지를 보낼 권한이 없습니다."

    if isinstance(exc, (UsernameInvalidError, UsernameNotOccupiedError,
                        UserNotParticipantError, InputUserDeactivatedError)):
        return DeliveryStatus.INVALID_RECIPIENT, "유효하지 않은 수신자입니다."

    if isinstance(exc, AccountNotAuthenticatedError):
        return DeliveryStatus.SESSION_EXPIRED, str(exc)

    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return DeliveryStatus.NETWORK_ERROR, "네트워크 오류가 발생했습니다. 다시 시도해주세요."

    if isinstance(exc, RPCError):
        return DeliveryStatus.PERMANENT_FAILURE, "텔레그램에서 요청을 처리할 수 없습니다."

    return DeliveryStatus.INTERNAL_ERROR, "내부 오류가 발생했습니다."


def _resolve_target(recipient: str) -> int | str:
    """Convert a recipient string to a Telethon-compatible target."""
    stripped = recipient.lstrip("-")
    return int(recipient) if stripped.isdigit() else recipient


async def _send_single(
    client: TelegramClient,
    target: int | str,
    message: str,
    media_path: str | None,
    reply_to_msg_id: int | None = None,
    inline_buttons: list[dict] | None = None,
) -> tuple[DeliveryStatus, int | None, str | None, int | None]:
    """Send a single message and return (status, telegram_msg_id, safe_error, flood_wait_seconds)."""
    started = datetime.now(timezone.utc)
    try:
        # Build inline keyboard markup if buttons are provided
        buttons = None
        if inline_buttons:
            rows = []
            for btn in inline_buttons:
                label = btn.get("label", "")
                url = btn.get("url", "")
                if label and url:
                    rows.append([KeyboardButtonUrl(text=label, url=url)])
            if rows:
                buttons = ReplyInlineMarkup(rows=rows)

        if media_path:
            send_coro = client.send_file(target, media_path, caption=message, reply_to=reply_to_msg_id, buttons=buttons)
        else:
            send_coro = client.send_message(target, message, reply_to=reply_to_msg_id, buttons=buttons)
        result = await asyncio.wait_for(send_coro, timeout=PER_MESSAGE_TIMEOUT_SECONDS)
        msg_id = result.id if hasattr(result, "id") else None
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        if elapsed > 5.0:
            logger.warning("delivery_slow_send", recipient=str(target), elapsed_seconds=round(elapsed, 1))
        return (DeliveryStatus.SUCCESS, msg_id, None, None)
    except asyncio.TimeoutError:
        logger.warning("delivery_send_timeout", recipient=str(target), timeout_seconds=PER_MESSAGE_TIMEOUT_SECONDS)
        return (DeliveryStatus.NETWORK_ERROR, None, "전송 응답이 지연되어 시간 초과되었습니다.", None)
    except Exception as exc:
        status, safe_error = classify_error(exc)
        flood_wait = exc.seconds if isinstance(exc, FloodWaitError) else None
        return (status, None, safe_error, flood_wait)


async def _persist_log(
    account_id: str,
    recipient: str,
    source: str,
    source_id: str | None,
    status: DeliveryStatus,
    success: bool,
    telegram_message_id: int | None,
    error_message: str | None,
    attempt_count: int,
    message_content: str | None = None,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
) -> None:
    """Persist a delivery attempt to MessageLog."""
    async with async_session_maker() as db:
        log = MessageLog(
            account_id=account_id,
            recipient=recipient,
            source=source,
            source_id=source_id,
            status=status.value,
            success=success,
            telegram_message_id=telegram_message_id,
            error_message=error_message,
            attempt_count=attempt_count,
            message_content=message_content,
            started_at=started_at,
            completed_at=completed_at,
        )
        db.add(log)
        await db.commit()


def _publish_event(
    event_type: str,
    result: DeliveryResult | None,
    source: str,
    source_id: str | None,
    on_status_change: Callable | None = None,
) -> None:
    """Publish a delivery lifecycle event.

    Uses the on_status_change callback for real-time notification.
    No separate EventBus/WebSocket infrastructure exists in this repository;
    the callback pattern allows integration without creating a second event system.
    """
    if on_status_change is None:
        return
    if result is None:
        return
    try:
        on_status_change(result)
    except Exception as exc:
        logger.warning(f"delivery_callback_failed event={event_type} recipient={result.recipient} error={exc}")


async def _deliver_with_retry(
    client: TelegramClient,
    target: int | str,
    recipient: str,
    message: str,
    media_path: str | None,
    source: str,
    source_id: str | None,
    account_id: str,
    on_status_change: Callable | None = None,
    reply_to_msg_id: int | None = None,
    inline_buttons: list[dict] | None = None,
) -> DeliveryResult:
    """Attempt delivery with retry for recoverable failures.

    Idempotency: each attempt creates a separate MessageLog row.
    The final authoritative state is the row with success=True.
    Multiple failure rows with no matching success row = definitive failure.
    """
    attempt = 0
    last_result: DeliveryResult | None = None

    while attempt < MAX_RETRIES:
        attempt += 1

        if attempt > 1 and on_status_change:
            _publish_event(EVENT_RETRYING, None, source, source_id, on_status_change)

        started_at = utcnow_naive()
        status, msg_id, safe_error, flood_wait = await _send_single(
            client, target, message, media_path, reply_to_msg_id, inline_buttons,
        )
        completed_at = utcnow_naive()

        result = DeliveryResult(
            status=status,
            recipient=recipient,
            telegram_message_id=msg_id,
            error_message=safe_error,
            flood_wait_seconds=flood_wait,
            attempt_count=attempt,
        )

        # Persist every attempt
        is_success = status == DeliveryStatus.SUCCESS
        await _persist_log(
            account_id=account_id,
            recipient=recipient,
            source=source,
            source_id=source_id,
            status=status,
            success=is_success,
            telegram_message_id=msg_id,
            error_message=safe_error,
            attempt_count=attempt,
            message_content=message,
            started_at=started_at,
            completed_at=completed_at,
        )

        if is_success:
            _publish_event(EVENT_SENT, result, source, source_id, on_status_change)
            return result

        if status not in RECOVERABLE_STATUSES:
            _publish_event(EVENT_FAILED, result, source, source_id, on_status_change)
            return result

        # Recoverable: wait and retry
        last_result = result
        wait_time = flood_wait if flood_wait else BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
        capped_wait = min(wait_time, MAX_WAIT_SECONDS)
        logger.info(
            "delivery_retry",
            recipient=recipient,
            attempt=attempt,
            status=status.value,
            wait_seconds=round(capped_wait, 1),
        )
        await asyncio.sleep(capped_wait)

    # All retries exhausted
    if last_result is not None:
        last_result.error_message = f"모든 재시도에 실패했습니다: {last_result.error_message}"
        last_result.attempt_count = MAX_RETRIES
        _publish_event(EVENT_FAILED, last_result, source, source_id, on_status_change)
        return last_result

    exhausted = DeliveryResult(
        status=DeliveryStatus.NETWORK_ERROR,
        recipient=recipient,
        error_message="모든 재시도가 실패했습니다.",
        attempt_count=MAX_RETRIES,
    )
    await _persist_log(
        account_id=account_id,
        recipient=recipient,
        source=source,
        source_id=source_id,
        status=DeliveryStatus.NETWORK_ERROR,
        success=False,
        telegram_message_id=None,
        error_message=exhausted.error_message,
        attempt_count=MAX_RETRIES,
        message_content=message,
    )
    _publish_event(EVENT_FAILED, exhausted, source, source_id, on_status_change)
    return exhausted


async def deliver_message(
    request: DeliveryRequest,
    on_status_change: Callable | None = None,
    client: TelegramClient | None = None,
) -> list[DeliveryResult]:
    """Deliver a message to all recipients with retry logic.

    This is the canonical delivery pipeline. All Telegram send paths
    should use this function.

    Tenant/account authorization must happen BEFORE calling this function.
    This function does NOT enforce tenant boundaries — it resolves the
    account from DB and sends. The calling layer (API route, service)
    is responsible for authorization via require_account_tenant_access().

    Args:
        request: The delivery request.
        on_status_change: Optional callback for real-time status updates.
        client: Optional pre-authorized TelegramClient. If provided, the
            function skips get_authorized_client() and uses this client
            directly — useful for batch callers (reply macro, random reply)
            that already hold a live client and want to avoid per-recipient
            pool lock acquisition.

    Returns:
        List of DeliveryResult, one per recipient.
    """
    results: list[DeliveryResult] = []

    # 1. Resolve account
    async with async_session_maker() as db:
        account = await account_crud.get_account(db, request.account_id)
        if account is None:
            for recipient in request.recipients:
                result = DeliveryResult(
                    status=DeliveryStatus.INTERNAL_ERROR,
                    recipient=recipient,
                    error_message="계정을 찾을 수 없습니다.",
                )
                await _persist_log(
                    account_id=request.account_id,
                    recipient=recipient,
                    source=request.source,
                    source_id=request.source_id,
                    status=DeliveryStatus.INTERNAL_ERROR,
                    success=False,
                    telegram_message_id=None,
                    error_message=result.error_message,
                    attempt_count=1,
                    message_content=request.message,
                )
                results.append(result)
            return results

        # Fast-fail for banned accounts — no Telegram call needed
        if account.status == "banned":
            for recipient in request.recipients:
                result = DeliveryResult(
                    status=DeliveryStatus.BANNED,
                    recipient=recipient,
                    error_message="계정이 텔레그램에서 차단되었습니다.",
                )
                await _persist_log(
                    account_id=request.account_id,
                    recipient=recipient,
                    source=request.source,
                    source_id=request.source_id,
                    status=DeliveryStatus.BANNED,
                    success=False,
                    telegram_message_id=None,
                    error_message=result.error_message,
                    attempt_count=1,
                    message_content=request.message,
                )
                results.append(result)
            return results

    # 2. Get authorized client (decrypts session, checks authorization)
    if client is None:
        try:
            client = await get_authorized_client(account)
        except AccountNotAuthenticatedError as exc:
            # Recovery: clear the invalid session so subsequent attempts fast-fail
            try:
                async with async_session_maker() as db:
                    account_reloaded = await account_crud.get_account(db, request.account_id)
                    if account_reloaded is not None:
                        await account_crud.mark_account_session_invalid(db, account_reloaded)
            except Exception as persist_err:
                logger.warning("session_invalidation_failed", account_id=request.account_id, error=str(persist_err))

            for recipient in request.recipients:
                result = DeliveryResult(
                    status=DeliveryStatus.SESSION_EXPIRED,
                    recipient=recipient,
                    error_message=str(exc),
                )
                await _persist_log(
                    account_id=request.account_id,
                    recipient=recipient,
                    source=request.source,
                    source_id=request.source_id,
                    status=DeliveryStatus.SESSION_EXPIRED,
                    success=False,
                    telegram_message_id=None,
                    error_message=result.error_message,
                    attempt_count=1,
                    message_content=request.message,
                )
                results.append(result)
            return results

    # 3. Send to each recipient with retry
    _publish_event(EVENT_SENDING, None, request.source, request.source_id, on_status_change)

    for i, recipient in enumerate(request.recipients):
        target = _resolve_target(recipient)
        reply_to = (
            request.reply_to_map.get(recipient)
            if request.reply_to_map is not None
            else request.reply_to_msg_id
        )
        result = await _deliver_with_retry(
            client=client,
            target=target,
            recipient=recipient,
            message=request.message,
            media_path=request.media_path,
            source=request.source,
            source_id=request.source_id,
            account_id=request.account_id,
            on_status_change=on_status_change,
            reply_to_msg_id=reply_to,
            inline_buttons=request.inline_buttons,
        )
        results.append(result)

        # If Telegram told us the account is banned, persist it so future
        # deliveries fast-fail without a network call.
        if result.status == DeliveryStatus.BANNED:
            try:
                async with async_session_maker() as db:
                    account_to_ban = await account_crud.get_account(db, request.account_id)
                    if account_to_ban is not None:
                        await account_crud.mark_account_banned(db, account_to_ban)
            except Exception as persist_err:
                logger.warning("banned_persistence_failed", account_id=request.account_id, error=str(persist_err))

        # Pacing delay between recipients (configurable per request)
        if i < len(request.recipients) - 1:
            await asyncio.sleep(request.inter_message_delay)

    return results
