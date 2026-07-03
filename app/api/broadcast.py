import json
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud import account as account_crud
from app.crud import broadcast as broadcast_crud
from app.database import get_db
from app.schemas.broadcast import BroadcastCreate, BroadcastRead
from app.services.broadcast_processor import process_broadcast
from app.services.media import save_broadcast_media

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
    image: Annotated[UploadFile | None, File()] = None,
    db: AsyncSession = Depends(get_db),
):
    try:
        recipients_list = json.loads(recipients)
    except json.JSONDecodeError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="recipients는 JSON 배열이어야 합니다.")

    try:
        payload = BroadcastCreate(
            account_id=account_id,
            message=message,
            recipients=recipients_list,
            scheduled_at=scheduled_at or None,
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


@router.get("/{broadcast_id}", response_model=BroadcastRead)
async def read_broadcast(broadcast_id: str, db: AsyncSession = Depends(get_db)):
    broadcast = await broadcast_crud.get_broadcast(db, broadcast_id)
    if broadcast is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="발송 작업을 찾을 수 없습니다.")
    return broadcast
