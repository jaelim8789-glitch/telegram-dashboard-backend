import json
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_identity, Identity, require_account_tenant_access
from app.config import settings
from app.core.logging import get_logger
from app.crud import account as account_crud
from app.crud import broadcast as broadcast_crud
from app.database import get_db
from app.schemas.broadcast import BroadcastCreate, BroadcastRead, RECURRING_INTERVAL_VALUES
from app.services.broadcast_processor import process_broadcast
from app.services.media import save_broadcast_media

logger = get_logger(__name__)

router = APIRouter(prefix="/api/broadcast", tags=["broadcast"])


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
    image: Annotated[UploadFile | None, File()] = None,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    await require_account_tenant_access(account_id, db, identity)

    try:
        recipients_list = json.loads(recipients)
    except json.JSONDecodeError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="recipients는 JSON 배열이어야 합니다.")

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

    try:
        payload = BroadcastCreate(
            account_id=account_id,
            message=message,
            recipients=recipients_list,
            scheduled_at=scheduled_at or None,
            recurring_interval_minutes=recurring_val,
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
    # retry_broadcast should succeed since we already checked status and retry_count,
    # but guard against race conditions.
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="재시도 처리 중 상태가 변경되었습니다. 다시 시도해주세요.",
        )

    logger.info("broadcast_retried", broadcast_id=broadcast_id, account_id=broadcast.account_id)
    return updated


@router.get("/{broadcast_id}", response_model=BroadcastRead)
async def read_broadcast(
    broadcast_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    broadcast = await broadcast_crud.get_broadcast(db, broadcast_id)
    if broadcast is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="발송 작업을 찾을 수 없습니다.")
    # Verify the broadcast's account belongs to the caller's tenant
    account = await account_crud.get_account(db, broadcast.account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="발송 작업을 찾을 수 없습니다.")
    await require_account_tenant_access(broadcast.account_id, db, identity)
    return broadcast


# ── Recurring broadcast endpoints ──────────────────────────────────


@router.get("/recurring", response_model=list[BroadcastRead])
async def read_recurring_broadcasts(
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Return all active (non-cancelled) recurring broadcasts, tenant-isolated."""
    return await broadcast_crud.list_recurring_broadcasts(db, identity=identity)


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

    # Verify the broadcast's account belongs to the caller's tenant
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
    return updated