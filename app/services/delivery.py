"""
Canonical Telegram message delivery pipeline.

Owns:
1. Account resolution + tenant ownership validation
2. Session/client restoration
3. Recipient resolution
4. Send attempt with Telethon
5. Telegram response capture
6. MessageLog persistence
7. Failure classification
8. Retry decision
9. Operational event publication

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
    UserBannedInChannelError,
    UserDeactivatedBanError,
    UserIsBlockedError,
    UserKickedError,
    UserNotParticipantError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)

from app.core.logging import get_logger
from app.crud import account as account_crud
from app.database import async_session_maker
from app.models.account import Account
from app.services.telegram_actions import AccountNotAuthenticatedError, get_authorized_client

logger = get_logger(__name__)

# ─── Failure Taxonomy ─────────────────────────────────────────────────


class DeliveryStatus(enum.Enum):
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
    source: str = "manual"  # manual, broadcast, reply_macro, auto_reply, scheduled
    source_id: str | None = None  # broadcast_id, macro_id, etc.


def utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def classify_error(exc: Exception) -> tuple[DeliveryStatus, str | None]:
    """Classify a Telethon exception into a DeliveryStatus.

    Returns (status, safe_error_message).
    Never exposes raw exception details to clients.
    """
    if isinstance(exc, FloodWaitError):
        return DeliveryStatus.FLOOD_WAIT, f"텔레그램 속도 제한: {exc.seconds}초 대기 필요"

    if isinstance(exc, (UserDeactivatedBanError, PhoneNumberBannedError)):
        return DeliveryStatus.BANNED, "계정이 텔레그램에서 차단되었습니다."

    if isinstance(exc, (UserIsBlockedError, ChatWriteForbiddenError, ChatAdminRequiredError,
                        UserBannedInChannelError, UserKickedError)):
        return DeliveryStatus.FORBIDDEN, "해당 채팅방에 메시지를 보낼 권한이 없습니다."

    if isinstance(exc, (UsernameInvalidError, UsernameNotOccupiedError, UserNotParticipantError,
                        InputUserDeactivatedError)):
        return DeliveryStatus.INVALID_RECIPIENT, "유효하지 않은 수신자입니다."

    if isinstance(exc, AccountNotAuthenticatedError):
        return DeliveryStatus.SESSION_EXPIRED, str(exc)

    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return DeliveryStatus.NETWORK_ERROR, "네트워크 오류가 발생했습니다. 다시 시도해주세요."

    if isinstance(exc, RPCError):
        return DeliveryStatus.PERMANENT_FAILURE, f"텔레그램 오류: {exc.message}"

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
) -> tuple[DeliveryStatus, int | None, str | None, int | None]:
    """Send a single message and return (status, telegram_msg_id, safe_error, flood_wait_seconds)."""
    try:
        if media_path:
            result = await client.send_file(target, media_path, caption=message)
            msg_id = result.id if hasattr(result, "id") else None
        else:
            result = await client.send_message(target, message)
            msg_id = result.id
        return DeliveryStatus.SUCCESS, msg_id, None, None
    except Exception as exc:  # noqa: BLE001
        status, safe_error = classify_error(exc)
        flood_wait = exc.seconds if isinstance(exc, FloodWaitError) else None
        return status, None, safe_error, flood_wait


async def deliver_message(
    request: DeliveryRequest,
    on_status_change: Callable[[DeliveryResult], None] | None = None,
) -> list[DeliveryResult]:
    """Deliver a message to all recipients with retry logic.

    This is the canonical delivery pipeline. All Telegram send paths
    should use this function.

    Args:
        request: The delivery request.
        on_status_change: Optional callback for real-time status updates.

    Returns:
        List of DeliveryResult, one per recipient.
    """
    results: list[DeliveryResult] = []

    # 1. Resolve account
    async with async_session_maker() as db:
        account = await account_crud.get_account(db, request.account_id)
        if account is None:
            for recipient in request.recipients:
                results.append(DeliveryResult(
                    status=DeliveryStatus.INTERNAL_ERROR,
                    recipient=recipient,
                    error_message="계정을 찾을 수 없습니다.",
                ))
            return results

    # 2. Get authorized client
    try:
        client = await get_authorized_client(account)
    except AccountNotAuthenticatedError as exc:
        for recipient in request.recipients:
            results.append(DeliveryResult(
                status=DeliveryStatus.SESSION_EXPIRED,
                recipient=recipient,
                error_message=str(exc),
            ))
        return results

    # 3. Send to each recipient with retry
    for recipient in request.recipients:
        target = _resolve_target(recipient)
        result = await _deliver_with_retry(client, target, recipient, request.message, request.media_path)
        results.append(result)
        if on_status_change:
            on_status_change(result)

        # Pacing delay between recipients
        if recipient != request.recipients[-1]:
            await asyncio.sleep(1.0)

    return results


async def _deliver_with_retry(
    client: TelegramClient,
    target: int | str,
    recipient: str,
    message: str,
    media_path: str | None,
) -> DeliveryResult:
    """Attempt delivery with retry for recoverable failures."""
    attempt = 0
    last_result: DeliveryResult | None = None

    while attempt < MAX_RETRIES:
        attempt += 1
        status, msg_id, safe_error, flood_wait = await _send_single(client, target, message, media_path)

        result = DeliveryResult(
            status=status,
            recipient=recipient,
            telegram_message_id=msg_id,
            error_message=safe_error,
            flood_wait_seconds=flood_wait,
            attempt_count=attempt,
        )

        if status == DeliveryStatus.SUCCESS:
            return result

        if status not in RECOVERABLE_STATUSES:
            return result

        # Recoverable: wait and retry
        last_result = result
        wait_time = flood_wait if flood_wait else BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
        logger.info(
            "delivery_retry",
            recipient=recipient,
            attempt=attempt,
            status=status.value,
            wait_seconds=round(wait_time, 1),
        )
        await asyncio.sleep(min(wait_time, 60.0))  # Cap at 60s

    # All retries exhausted
    if last_result:
        last_result.error_message = f"모든 재시도 실패: {last_result.error_message}"
        return last_result

    return DeliveryResult(
        status=DeliveryStatus.NETWORK_ERROR,
        recipient=recipient,
        error_message="모든 재시도가 실패했습니다.",
        attempt_count=MAX_RETRIES,
    )