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

from __future__ import annotations

import asyncio
import enum
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from typing import Any, Callable

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
from telethon.tl.types import ReplyInlineMarkup, KeyboardButtonUrl, InputFile

from app.core.logging import get_logger
from app.core.time import utcnow_naive
from app.crud import account as account_crud
from app.database import async_session_maker
from app.models.account import Account
from app.models.message_log import MessageLog
from app.services.telegram_actions import AccountNotAuthenticatedError, get_authorized_client
from app.services.telethon_pool import is_account_flood_limited, record_flood_wait

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

WATERMARK_AD = (
    "\n\n━━━━━━━━━━━━━━━━━━\n"
    "🤖 AI가 자동으로 답변했습니다. 무료 AI 직원 받기\n\n"
    "🌐 https://telemon.online/signup?ref={ref_code}"
)

FREE_PLANS = {"free"}

_watermark_ad_cache: str | None = None
_watermark_cache_expiry: float = 0.0
_watermark_format_cache: dict[str, str | None] = {}

async def _get_watermark_ad() -> str:
    """Load watermark from DB setting, with fallback to default. Cached for 60s."""
    global _watermark_ad_cache, _watermark_cache_expiry
    now = __import__("time").time()
    if _watermark_ad_cache is not None and now < _watermark_cache_expiry:
        return _watermark_ad_cache

    default = (
        "\n\n━━━━━━━━━━━━━━━━━━\n"
        "🤖 AI가 자동으로 답변했습니다. 무료 AI 직원 받기\n\n"
        "🌐 https://telemon.online/signup?ref={ref_code}"
    )
    try:
        async with async_session_maker() as session:
            from app.models.system_setting import SystemSetting
            from sqlalchemy import select as sa_sel
            stmt = sa_sel(SystemSetting).where(SystemSetting.key == "watermark_ad")
            result = await session.execute(stmt)
            setting = result.scalar_one_or_none()
            if setting and setting.value is not None:
                _watermark_ad_cache = setting.value if setting.value.strip() else ""
            else:
                _watermark_ad_cache = default
    except Exception:
        _watermark_ad_cache = default

    _watermark_cache_expiry = now + 60.0
    return _watermark_ad_cache


async def _get_tenant_ref_code(db: AsyncSession, tenant_id: str) -> str | None:
    """Look up the tenant's active referral code."""
    from app.models.referral import ReferralCode
    from sqlalchemy import select as sa_sel
    result = await db.execute(
        sa_sel(ReferralCode.code).where(
            ReferralCode.owner_id == tenant_id,
            ReferralCode.is_active == True,
        )
    )
    return result.scalar_one_or_none()


async def _personalize_watermark(base: str, ref_code: str | None) -> str:
    """Replace {ref_code} placeholder in the watermark template."""
    if not base:
        return ""
    code = ref_code or ""
    return base.replace("{ref_code}", code)

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
    batch_size: int | None = None
    tenant_plan: str | None = None


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


# ─── Restriction early-warning ───────────────────────────────────────────
#
# When an account starts getting `forbidden`-class errors (no write permission /
# not a member / kicked / blocked) across MANY DISTINCT recipients in a short
# window, it usually means Telegram has flagged the account for spam-like mass
# sending — not a single bad group. Continuing to hammer it risks escalating a
# temporary restriction into a permanent ban. We detect this pattern and pause
# the account's sending as a protective cool-down.
#
# Detection is in-memory (per process) to avoid a DB round-trip on every
# recipient. State is best-effort: if the process restarts mid-burst the worst
# case is we re-detect it on the next burst. The authoritative pause is the
# account's DB row (status='suspended'), which the delivery pipeline honors via
# fast-fail just like 'banned'.

RESTRICTION_WINDOW_MINUTES = 10
RESTRICTION_DISTINCT_RECIPIENTS = 5

# account_id -> list[(timestamp, recipient)] of recent forbidden failures
_forbidden_bursts: dict[str, list[tuple[datetime, str]]] = {}

RESTRICTION_WARNING = (
    "이 계정이 텔레그램 제재를 받았을 수 있습니다. 발송을 일시 중단했습니다."
)


def _record_forbidden_failure(account_id: str, recipient: str) -> bool:
    """Record a forbidden failure and return True if the account should be
    suspended (distinct-recipient threshold exceeded within the window)."""
    now = utcnow_naive()
    cutoff = now - timedelta(minutes=RESTRICTION_WINDOW_MINUTES)
    burst = _forbidden_bursts.setdefault(account_id, [])
    burst.append((now, recipient))
    # Drop entries outside the window.
    burst[:] = [(ts, r) for ts, r in burst if ts >= cutoff]
    distinct = {r for _, r in burst}
    return len(distinct) >= RESTRICTION_DISTINCT_RECIPIENTS


async def _maybe_suspend_for_restriction(account_id: str) -> None:
    """Pause sending for an account that shows a restriction pattern.

    No-op if the account is already suspended/banned (don't clobber state) or
    if suspension persistence fails (the in-memory detector will retry on the
    next failure).
    """
    try:
        async with async_session_maker() as db:
            account = await account_crud.get_account(db, account_id)
            if account is None:
                return
            if account.status in ("suspended", "banned"):
                return
            await account_crud.suspend_account_for_restriction(
                db, account, RESTRICTION_WARNING
            )
            logger.warning(
                "account_suspended_for_restriction",
                account_id=account_id,
                window_minutes=RESTRICTION_WINDOW_MINUTES,
                distinct_recipients_threshold=RESTRICTION_DISTINCT_RECIPIENTS,
            )
    except Exception as exc:
        logger.warning(
            "restriction_suspension_failed", account_id=account_id, error=str(exc)
        )


def _resolve_target(recipient: str) -> int | str:
    """Convert a recipient string to a Telethon-compatible target."""
    stripped = recipient.lstrip("-")
    return int(recipient) if stripped.isdigit() else recipient


async def _send_single(
    client: TelegramClient,
    target: int | str,
    message: str,
    media_path: str | None,
    uploaded_file: InputFile | None = None,
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

        if uploaded_file is not None:
            send_coro = client.send_file(target, uploaded_file, caption=message, reply_to=reply_to_msg_id, buttons=buttons)
        elif media_path:
            send_coro = client.send_file(target, media_path, caption=message, reply_to=reply_to_msg_id, buttons=buttons)
        else:
            send_coro = client.send_message(target, message, reply_to=reply_to_msg_id, buttons=buttons)
        
        # 타임아웃 시간을 줄여서 빠르게 실패 처리
        result = await asyncio.wait_for(send_coro, timeout=PER_MESSAGE_TIMEOUT_SECONDS)
        msg_id = result.id if hasattr(result, "id") else None
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        
        # 로그 기록 시 시간이 오래 걸리는 경우 표시
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


# ─── Batch persist state (shared across calls within a single deliver_message run) ──
# Collects log rows in memory and flushes in batches to avoid per-recipient DB sessions.

_MAX_BATCH_SIZE = 100


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
    """Persist a single delivery attempt — used when persistence is needed outside
    a batch context (e.g. fast-fail paths). Prefer the batch flush when possible."""
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
    uploaded_file: InputFile | None = None,
    source: str = "",
    source_id: str | None = None,
    account_id: str = "",
    on_status_change: Callable | None = None,
    reply_to_msg_id: int | None = None,
    inline_buttons: list[dict] | None = None,
    batch_persister: _BatchPersister | None = None,
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
            client, target, message, media_path, uploaded_file, reply_to_msg_id, inline_buttons,
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
        if batch_persister is not None:
            await batch_persister.add(
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
        else:
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
    if batch_persister is not None:
        await batch_persister.add(
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
    else:
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


class _BatchPersister:
    """Collects MessageLog rows in memory and flushes in batches to avoid
    per-recipient DB sessions. Within a single deliver_message() call this
    reduces DB round-trips from N to ceil(N/100)."""

    def __init__(self) -> None:
        self._rows: list[MessageLog] = []

    async def add(
        self,
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
        self._rows.append(MessageLog(
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
        ))

    async def flush(self, force: bool = False) -> None:
        if not self._rows:
            return
        if not force and len(self._rows) < _MAX_BATCH_SIZE:
            return
        batch = self._rows
        self._rows = []
        async with async_session_maker() as db:
            db.add_all(batch)
            await db.commit()

    async def flush_all(self) -> None:
        await self.flush(force=True)


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

        # Fast-fail for suspended accounts (protective cool-down after a likely
        # Telegram restriction) — don't push the (possibly restricted) account further.
        if account.status == "suspended":
            for recipient in request.recipients:
                result = DeliveryResult(
                    status=DeliveryStatus.FORBIDDEN,
                    recipient=recipient,
                    error_message=RESTRICTION_WARNING,
                )
                await _persist_log(
                    account_id=request.account_id,
                    recipient=recipient,
                    source=request.source,
                    source_id=request.source_id,
                    status=DeliveryStatus.FORBIDDEN,
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

        tenant_plan = None
        if account.tenant_id:
            from app.models.tenant import Tenant
            tenant = await db.get(Tenant, account.tenant_id)
            if tenant is not None:
                tenant_plan = tenant.plan
        request.tenant_plan = tenant_plan

    # 2. Fast-fail if account is globally flood-limited
    limited, remaining = is_account_flood_limited(request.account_id)
    if limited:
        for recipient in request.recipients:
            result = DeliveryResult(
                status=DeliveryStatus.FLOOD_WAIT,
                recipient=recipient,
                error_message=f"텔레그램 속도 제한: {remaining:.0f}초 대기 필요",
                flood_wait_seconds=int(remaining),
            )
            await _persist_log(
                account_id=request.account_id,
                recipient=recipient,
                source=request.source,
                source_id=request.source_id,
                status=DeliveryStatus.FLOOD_WAIT,
                success=False,
                telegram_message_id=None,
                error_message=result.error_message,
                attempt_count=1,
                message_content=request.message,
            )
            results.append(result)
        return results

    # 3. Get authorized client (decrypts session, checks authorization)
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

    # 4. Upload media once so all recipients share the same server-side file handle
    uploaded_file: InputFile | None = None
    if request.media_path:
        upload_start = datetime.now(timezone.utc)
        uploaded_file = await client.upload_file(request.media_path)
        upload_elapsed = (datetime.now(timezone.utc) - upload_start).total_seconds()
        logger.info(
            "media_uploaded_once",
            media_path=request.media_path,
            elapsed_seconds=round(upload_elapsed, 2),
        )

    # 5. Send to each recipient with retry
    _publish_event(EVENT_SENDING, None, request.source, request.source_id, on_status_change)
    entity_cache: dict[str, Any] = {}
    batch_persister = _BatchPersister()

    async def _resolve_target_entity(recipient: str) -> Any:
        """Resolve recipient target once and cache it for retries/reuses.

        Telethon usually caches entity lookups internally, but repeated send/retry
        paths still pay resolver overhead in hot loops. Caching here keeps the
        delivery loop stable under retry storms and large recipient sets.
        """
        cached = entity_cache.get(recipient)
        if cached is not None:
            return cached

        base_target = _resolve_target(recipient)
        try:
            resolved = await client.get_input_entity(base_target)
            entity_cache[recipient] = resolved
            return resolved
        except Exception:
            # Fall back to the raw target when entity resolution is unavailable.
            return base_target

    # 6. Adaptive throttling state (FloodWait-aware pacing)
    adaptive_delay = request.inter_message_delay
    original_delay = request.inter_message_delay
    consecutive_ok = 0

    async def _send_one(recipient: str) -> DeliveryResult:
        """Send to a single recipient with post-send bookkeeping."""
        nonlocal adaptive_delay, consecutive_ok
        target = await _resolve_target_entity(recipient)
        reply_to = (
            request.reply_to_map.get(recipient)
            if request.reply_to_map is not None
            else request.reply_to_msg_id
        )
        message_to_send = request.message
        if request.tenant_plan in FREE_PLANS:
            watermark = await _get_watermark_ad()
            ref_code = await _get_tenant_ref_code(db, account.tenant_id) if account.tenant_id else None
            personalized = await _personalize_watermark(watermark, ref_code)
            message_to_send = request.message + personalized
        result = await _deliver_with_retry(
            client=client,
            target=target,
            recipient=recipient,
            message=message_to_send,
            media_path=request.media_path,
            uploaded_file=uploaded_file,
            source=request.source,
            source_id=request.source_id,
            account_id=request.account_id,
            on_status_change=on_status_change,
            reply_to_msg_id=reply_to,
            inline_buttons=request.inline_buttons,
            batch_persister=batch_persister,
        )

        # Adaptive throttling: adjust pacing based on FloodWait feedback
        if result.status == DeliveryStatus.FLOOD_WAIT and result.flood_wait_seconds and result.flood_wait_seconds > 0:
            record_flood_wait(request.account_id, result.flood_wait_seconds)
            adaptive_delay = min(adaptive_delay * 1.5, 15.0)
            consecutive_ok = 0
            logger.info(
                "adaptive_delay_increased",
                new_delay=round(adaptive_delay, 2),
                flood_wait=result.flood_wait_seconds,
            )
        elif result.status == DeliveryStatus.SUCCESS:
            consecutive_ok += 1
            if consecutive_ok >= 5:
                adaptive_delay = max(adaptive_delay * 0.9, original_delay)
                consecutive_ok = 0
                logger.info(
                    "adaptive_delay_decayed",
                    new_delay=round(adaptive_delay, 2),
                )

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

        # Restriction early-warning
        if result.status == DeliveryStatus.FORBIDDEN:
            should_suspend = _record_forbidden_failure(request.account_id, recipient)
            if should_suspend:
                await _maybe_suspend_for_restriction(request.account_id)

        return result

    batch_size = request.batch_size or 1
    total = len(request.recipients)

    if batch_size > 1 and total > 1:
        # Batch mode: send N recipients concurrently, then pace between batches
        for start in range(0, total, batch_size):
            batch = request.recipients[start:start + batch_size]
            batch_results = await asyncio.gather(
                *(_send_one(r) for r in batch),
                return_exceptions=True,
            )
            for r in batch_results:
                if isinstance(r, Exception):
                    logger.error("batch_send_exception", error=str(r))
                    continue
                results.append(r)
            # Pacing delay between batches (not between individual sends)
            if start + batch_size < total:
                await asyncio.sleep(adaptive_delay)
    else:
        # Sequential mode (default): send one at a time with pacing
        for i, recipient in enumerate(request.recipients):
            try:
                result = await _send_one(recipient)
                results.append(result)
            except Exception as exc:
                logger.error("sequential_send_exception", recipient=recipient, error=str(exc))
            if i < total - 1 and adaptive_delay > 0:
                await asyncio.sleep(adaptive_delay)

    await batch_persister.flush_all()
    return results
