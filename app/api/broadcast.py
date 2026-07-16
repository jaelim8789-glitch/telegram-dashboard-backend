import json
from datetime import datetime, timezone
from typing import Annotated

from fastapi import Request, APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_identity, Identity, require_account_tenant_access, require_broadcast_capacity
from app.config import settings
from app.core.logging import get_logger
from app.crud import account as account_crud
from app.crud import broadcast as broadcast_crud
from app.database import get_db
from app.schemas.broadcast import (
    BroadcastChildrenRead,
    BroadcastCreate,
    BroadcastRead,
    BroadcastSendGroupRequest,
    BatchRetryRequest,
    BatchRetryResult,
    BroadcastEstimateRequest,
    BroadcastEstimateResponse,
    RECURRING_INTERVAL_VALUES,
    DeliveryMode,
)
from app.services.broadcast_processor import process_broadcast
from app.services.failure_intel import classify_failure
from app.services.media import save_broadcast_media

logger = get_logger(__name__)

router = APIRouter(prefix="/api/broadcast", tags=["broadcast"])


def _enrich_broadcast(broadcast):
    """Add failure_info to a Broadcast ORM object for API response."""
    if broadcast is not None and broadcast.status == "failed" and broadcast.error_message:
        broadcast.failure_info = classify_failure(broadcast.status, broadcast.error_message)
    return broadcast


def _enrich_broadcast_list(broadcasts: list) -> list:
    for b in broadcasts:
        _enrich_broadcast(b)
    return broadcasts


def _to_naive_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


@router.post("", response_model=BroadcastRead, status_code=status.HTTP_202_ACCEPTED)
async def create_broadcast(
    background_tasks: BackgroundTasks,
    account_id: Annotated[str, Form()],
    message: Annotated[str, Form()],
    recipients: Annotated[str, Form(description="JSON array of recipient chat ids, e.g. [\"-100123\"]")],
    scheduled_at: Annotated[
        str | None, Form(description="ISO 8601 datetime — omit or leave blank to send immediately")
    ] = None,
    recurring_interval_minutes: Annotated[
        str | None, Form(description="Minutes between recurring sends. One of: 30, 60, 120, 180, 360, 720, 1440")
    ] = None,
    delivery_mode: Annotated[
        str | None, Form(description="Delivery mode: normal (1min/group), cycle (round-robin), bulk (instant all), reply (reply to latest message)")
    ] = None,
    reply_to_message_id: Annotated[
        str | None, Form(description="Message ID to reply to (only used when delivery_mode is 'reply')")
    ] = None,
    delay_seconds: Annotated[
        str | None, Form(description="Per-recipient pacing override in seconds for delivery_mode 'normal' (e.g. 5, 10, 30, 60)")
    ] = None,
    inline_buttons: Annotated[
        str | None, Form(description="JSON array of inline buttons, e.g. [{\"label\":\"홈페이지\",\"url\":\"https://...\"}]")
    ] = None,
    group_ids: Annotated[
        str | None, Form(description="JSON array of group chat IDs to resolve recipients from, e.g. [\"-100123\"]")
    ] = None,
    campaign_id: Annotated[
        str | None, Form(description="Campaign ID to link this broadcast to")
    ] = None,
    image: Annotated[UploadFile | None, File()] = None,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    await require_account_tenant_access(account_id, db, identity)
    await require_broadcast_capacity(db, identity)

    try:
        recipients_list = json.loads(recipients) if recipients else []
    except json.JSONDecodeError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="recipients는 JSON 배열이어야 합니다.")

    # Parse group_ids
    parsed_group_ids: list[str] | None = None
    if group_ids is not None and group_ids.strip():
        try:
            parsed_list = json.loads(group_ids.strip())
            if not isinstance(parsed_list, list):
                raise ValueError("group_ids must be a JSON array")
            parsed_group_ids = [str(g) for g in parsed_list]
        except (json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"group_ids 형식이 올바르지 않습니다: {exc}",
            )

    # Parse recurring_interval_minutes
    recurring_val: int | None = None
    if recurring_interval_minutes is not None and recurring_interval_minutes.strip():
        try:
            recurring_val = int(recurring_interval_minutes.strip())
            if recurring_val not in RECURRING_INTERVAL_VALUES:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(
                        f"recurring_interval_minutes는 {sorted(RECURRING_INTERVAL_VALUES)} 중 하나여야 합니다. "
                        f"입력값: {recurring_val}"
                    ),
                )
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="recurring_interval_minutes는 유효한 정수여야 합니다.",
            )

    # Parse delivery_mode
    mode_val: DeliveryMode = "normal"
    if delivery_mode is not None and delivery_mode.strip():
        if delivery_mode.strip() not in ("normal", "cycle", "bulk", "reply"):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="delivery_mode는 normal, cycle, bulk, reply 중 하나여야 합니다.",
            )
        mode_val = delivery_mode.strip()

    # Parse reply_to_message_id
    parsed_reply_to_id: int | None = None
    if reply_to_message_id is not None and reply_to_message_id.strip():
        try:
            parsed_reply_to_id = int(reply_to_message_id.strip())
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="reply_to_message_id는 유효한 정수여야 합니다.",
            )

    # Parse delay_seconds
    parsed_delay_seconds: int | None = None
    if delay_seconds is not None and delay_seconds.strip():
        try:
            parsed_delay_seconds = int(delay_seconds.strip())
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="delay_seconds는 유효한 정수여야 합니다.",
            )

    # Parse inline_buttons
    parsed_inline_buttons: list[dict] | None = None
    if inline_buttons is not None and inline_buttons.strip():
        try:
            parsed_list = json.loads(inline_buttons.strip())
            if not isinstance(parsed_list, list):
                raise ValueError("inline_buttons must be a JSON array")
            for btn in parsed_list:
                if not isinstance(btn, dict) or "label" not in btn or "url" not in btn:
                    raise ValueError("each button must have 'label' and 'url' fields")
            parsed_inline_buttons = parsed_list
        except (json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"inline_buttons 형식이 올바르지 않습니다: {exc}",
            )

    # Validate: need at least recipients or group_ids
    if not recipients_list and not parsed_group_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="recipients 또는 group_ids 중 하나는 필수입니다.",
        )

    try:
        payload = BroadcastCreate(
            account_id=account_id,
            message=message,
            recipients=recipients_list,
            scheduled_at=scheduled_at or None,
            recurring_interval_minutes=recurring_val,
            delivery_mode=mode_val,
            reply_to_msg_id=parsed_reply_to_id,
            delay_seconds=parsed_delay_seconds,
            inline_buttons=parsed_inline_buttons,
            group_ids=parsed_group_ids,
            campaign_id=campaign_id,
        )
    except ValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.errors())

    account = await account_crud.get_account(db, payload.account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="계정을 찾을 수 없습니다.")

    now = broadcast_crud.utcnow_naive()
    scheduled_for = _to_naive_utc(payload.scheduled_at) if payload.scheduled_at else None
    is_immediate = scheduled_for is None or scheduled_for <= now

    if is_immediate:
        wait_seconds = await broadcast_crud.seconds_until_next_allowed_broadcast(db, payload.account_id)
        if wait_seconds > 0:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"발송 제한: {int(wait_seconds) + 1}초 후 다시 시도해주세요 (계정당 1분에 1회).",
            )

    media_path = await save_broadcast_media(image) if image is not None else None

    broadcast = await broadcast_crud.create_broadcast(
        db, payload, media_path, scheduled_at=None if is_immediate else scheduled_for
    )

    if is_immediate:
        background_tasks.add_task(process_broadcast, broadcast.id)

    return broadcast


# ── Send-to-Group endpoint ─────────────────────────────────────────


@router.post("/send-group", response_model=BroadcastRead, status_code=status.HTTP_202_ACCEPTED)
async def send_to_group(
    payload: BroadcastSendGroupRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Send a broadcast to all members of one or more Telegram groups.

    Group member lists are resolved at dispatch time (not creation time),
    ensuring fresh recipient lists. The resolved member IDs are stored in
    ``recipients`` after resolution.
    """
    await require_account_tenant_access(payload.account_id, db, identity)
    await require_broadcast_capacity(db, identity)

    account = await account_crud.get_account(db, payload.account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="계정을 찾을 수 없습니다.")

    now = broadcast_crud.utcnow_naive()
    scheduled_for = _to_naive_utc(payload.scheduled_at) if payload.scheduled_at else None
    is_immediate = scheduled_for is None or scheduled_for <= now

    if is_immediate:
        wait_seconds = await broadcast_crud.seconds_until_next_allowed_broadcast(db, payload.account_id)
        if wait_seconds > 0:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"발송 제한: {int(wait_seconds) + 1}초 후 다시 시도해주세요 (계정당 1분에 1회).",
            )

    try:
        create_payload = BroadcastCreate(
            account_id=payload.account_id,
            message=payload.message,
            recipients=[],
            scheduled_at=payload.scheduled_at,
            delivery_mode=payload.delivery_mode,
            delay_seconds=payload.delay_seconds,
            inline_buttons=payload.inline_buttons,
            group_ids=payload.group_ids,
            campaign_id=payload.campaign_id,
        )
    except ValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.errors())

    broadcast = await broadcast_crud.create_broadcast(
        db, create_payload, media_path=None, scheduled_at=None if is_immediate else scheduled_for
    )

    if is_immediate:
        background_tasks.add_task(process_broadcast, broadcast.id)

    return broadcast


# ── Broadcast estimate ─────────────────────────────────────────────


@router.post("/estimate", response_model=BroadcastEstimateResponse)
async def estimate_broadcast_delivery(
    payload: BroadcastEstimateRequest,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Estimate how long a broadcast will take to deliver.

    Accounts for delivery mode, inter-message delay, and the 1-minute
    per-account rate limit. Returns estimated time in seconds and a
    human-readable string.
    """
    await require_account_tenant_access(payload.account_id, db, identity)

    account = await account_crud.get_account(db, payload.account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="계정을 찾을 수 없습니다.")

    n = payload.recipient_count

    if payload.delivery_mode == "bulk":
        # Bulk: 0.3s per recipient
        per_recipient = 0.3
    elif payload.delivery_mode == "reply":
        per_recipient = 1.0
    elif payload.delay_seconds is not None:
        per_recipient = float(payload.delay_seconds)
    else:
        per_recipient = 60.0  # Normal mode: 1 per minute due to rate limit

    # First message can be sent immediately; subsequent ones have delays
    total_seconds = int((n - 1) * per_recipient) if n > 1 else 1
    total_seconds = max(total_seconds, 1)

    minutes = total_seconds // 60
    seconds = total_seconds % 60

    if minutes > 0:
        readable = f"약 {minutes}분 {seconds}초 ({total_seconds}초)"
    else:
        readable = f"약 {seconds}초"

    return BroadcastEstimateResponse(
        estimated_seconds=total_seconds,
        estimated_minutes=minutes,
        readable=readable,
    )


# ── Batch retry ────────────────────────────────────────────────────


@router.post("/batch-retry", response_model=BatchRetryResult)
async def batch_retry_broadcasts(
    payload: BatchRetryRequest,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Retry multiple failed broadcasts at once.

    Accepts a list of broadcast IDs and retries each one if it's in "failed"
    status and has not exceeded the retry limit. Returns per-ID results.
    """
    results = await broadcast_crud.batch_retry_broadcasts(db, payload.broadcast_ids, identity)
    return BatchRetryResult(results=results)


@router.post("/{broadcast_id}/retry", response_model=BroadcastRead)
async def retry_broadcast(
    broadcast_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Reset a failed broadcast to pending so it can be re-dispatched.

    Only works if the broadcast is currently in ``"failed"`` status and the
    retry limit (``broadcast_max_retries``, default 3) has not been reached.
    Clears the error message and ``sent_at`` timestamp.  The next scheduler
    tick (or a manual call to ``process_broadcast``) will pick it up.

    Tenant access is verified before the state transition.
    """
    broadcast = await broadcast_crud.get_broadcast(db, broadcast_id)
    if broadcast is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="발송 작업을 찾을 수 없습니다.")

    # Recurring parent records are templates, not occurrences — cannot be retried
    if broadcast.recurring_interval_minutes is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="반복 발송 시리즈 자체는 재시도할 수 없습니다. 필요 시 새 반복 발송을 생성해주세요.",
        )

    # Verify the broadcast's account belongs to the caller's tenant
    account = await account_crud.get_account(db, broadcast.account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="발송 작업을 찾을 수 없습니다.")
    await require_account_tenant_access(broadcast.account_id, db, identity)

    if broadcast.status != "failed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"재시도할 수 없는 상태입니다 (현재: {broadcast.status}). 실패한 발송만 재시도 가능합니다.",
        )

    if broadcast.retry_count >= settings.broadcast_max_retries:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"최대 재시도 횟수({settings.broadcast_max_retries}회)에 도달했습니다. "
                "새로운 발송을 생성해주세요."
            ),
        )

    updated = await broadcast_crud.retry_broadcast(db, broadcast_id)
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="재시도 처리 중 상태가 변경되었습니다. 다시 시도해주세요.",
        )

    logger.info("broadcast_retried", broadcast_id=broadcast_id, account_id=broadcast.account_id)
    return _enrich_broadcast(updated)



# ── Recurring broadcast endpoints ──────────────────────────────────
# Static routes must be declared before parameterised routes so that
# e.g. "/recurring" is not captured by "/{broadcast_id}".


@router.get("/recurring", response_model=list[BroadcastRead])
async def read_recurring_broadcasts(
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Return all active (non-cancelled) recurring broadcasts, tenant-isolated."""
    return _enrich_broadcast_list(await broadcast_crud.list_recurring_broadcasts(db, identity=identity))


@router.get("/{broadcast_id}", response_model=BroadcastRead)
async def read_broadcast(
    broadcast_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    broadcast = await broadcast_crud.get_broadcast(db, broadcast_id)
    if broadcast is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="발송 작업을 찾을 수 없습니다.")
    account = await account_crud.get_account(db, broadcast.account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="발송 작업을 찾을 수 없습니다.")
    await require_account_tenant_access(broadcast.account_id, db, identity)
    return _enrich_broadcast(broadcast)


@router.post("/dispatch/{broadcast_id}", response_model=BroadcastRead)
async def dispatch_broadcast(
    broadcast_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    broadcast = await broadcast_crud.get_broadcast(db, broadcast_id)
    if broadcast is None:
        raise HTTPException(status_code=404, detail="broadcast not found")
    await require_account_tenant_access(broadcast.account_id, db, identity)
    if broadcast.recurring_interval_minutes is not None:
        raise HTTPException(status_code=409, detail="recurring cannot use send-now")
    from app.services.broadcast_processor import process_broadcast
    from app.crud.broadcast import retry_broadcast as crud_retry
    updated = await crud_retry(db, broadcast_id)
    if updated is None:
        raise HTTPException(status_code=409, detail="retry failed")
    await db.commit()
    await db.refresh(updated)
    await process_broadcast(updated.id)
    await db.refresh(updated)
    logger.info("broadcast_send_now", broadcast_id=broadcast_id)
    return _enrich_broadcast(updated)


# ── Cancel broadcast ──────────────────────────────────────────────


@router.post("/{broadcast_id}/cancel", response_model=BroadcastRead)
async def cancel_broadcast(
    broadcast_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Cancel a recurring broadcast.

    Sets status to 'cancelled' and records cancelled_at.
    The scheduler will never dispatch this broadcast again.
    Only works on broadcasts that have recurring_interval_minutes set.
    """
    broadcast = await broadcast_crud.get_broadcast(db, broadcast_id)
    if broadcast is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="발송 작업을 찾을 수 없습니다.")

    await require_account_tenant_access(broadcast.account_id, db, identity)

    if broadcast.recurring_interval_minutes is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="반복 발송이 아닌 작업은 취소할 수 없습니다.",
        )

    if broadcast.status == "cancelled":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="이미 취소된 반복 발송입니다.",
        )

    updated = await broadcast_crud.cancel_recurring_broadcast(db, broadcast_id)
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="취소 처리 중 상태가 변경되었습니다. 다시 시도해주세요.",
        )

    logger.info(
        "recurring_broadcast_cancelled",
        broadcast_id=broadcast_id,
        account_id=broadcast.account_id,
    )
    return _enrich_broadcast(updated)


@router.post("/{broadcast_id}/pause", response_model=BroadcastRead)
async def pause_broadcast(
    broadcast_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Pause a recurring broadcast. The scheduler skips paused broadcasts
    but preserves the schedule config. Only works on recurring broadcasts."""
    broadcast = await broadcast_crud.get_broadcast(db, broadcast_id)
    if broadcast is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="발송 작업을 찾을 수 없습니다.")

    await require_account_tenant_access(broadcast.account_id, db, identity)

    if broadcast.recurring_interval_minutes is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="반복 발송이 아닌 작업은 일시중지할 수 없습니다.",
        )

    if broadcast.status == "cancelled":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="취소된 반복 발송은 일시중지할 수 없습니다.",
        )

    if broadcast.is_recurring_paused:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="이미 일시중지된 반복 발송입니다.",
        )

    updated = await broadcast_crud.pause_recurring_broadcast(db, broadcast_id)
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="일시중지 처리 중 상태가 변경되었습니다. 다시 시도해주세요.",
        )

    logger.info("recurring_broadcast_paused", broadcast_id=broadcast_id, account_id=broadcast.account_id)
    return _enrich_broadcast(updated)


@router.post("/{broadcast_id}/unpause", response_model=BroadcastRead)
async def unpause_broadcast(
    broadcast_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Unpause a recurring broadcast. The scheduler resumes dispatching it
    on its next scheduled run. Only works on paused recurring broadcasts."""
    broadcast = await broadcast_crud.get_broadcast(db, broadcast_id)
    if broadcast is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="발송 작업을 찾을 수 없습니다.")

    await require_account_tenant_access(broadcast.account_id, db, identity)

    if broadcast.recurring_interval_minutes is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="반복 발송이 아닌 작업은 재개할 수 없습니다.",
        )

    if broadcast.status == "cancelled":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="취소된 반복 발송은 재개할 수 없습니다.",
        )

    if not broadcast.is_recurring_paused:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="일시중지되지 않은 반복 발송입니다.",
        )

    updated = await broadcast_crud.unpause_recurring_broadcast(db, broadcast_id)
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="재개 처리 중 상태가 변경되었습니다. 다시 시도해주세요.",
        )

    logger.info("recurring_broadcast_unpaused", broadcast_id=broadcast_id, account_id=broadcast.account_id)
    return _enrich_broadcast(updated)


@router.get("/{broadcast_id}/children", response_model=list[BroadcastChildrenRead])
async def read_recurring_children(
    broadcast_id: str,
    limit: int = 20,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Get execution history (child broadcasts) for a recurring parent broadcast."""
    broadcast = await broadcast_crud.get_broadcast(db, broadcast_id)
    if broadcast is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="발송 작업을 찾을 수 없습니다.")

    await require_account_tenant_access(broadcast.account_id, db, identity)

    if broadcast.recurring_interval_minutes is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="반복 발송이 아닌 작업입니다.",
        )

    return _enrich_broadcast_list(await broadcast_crud.list_child_broadcasts(db, broadcast_id, limit=limit, offset=offset))