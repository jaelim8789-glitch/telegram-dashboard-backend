import json

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_identity, Identity, require_account_tenant_access
from app.core.logging import get_logger
from app.crud import account as account_crud
from app.crud import reply_macro as macro_crud
from app.database import get_db
from app.schemas.reply_macro import ReplyMacroCreate, ReplyMacroLogRead, ReplyMacroRead, ReplyMacroUpdate
from app.services.media import save_broadcast_media
from app.services.reply_macro_service import execute_reply_macro

router = APIRouter(prefix="/api/accounts/{account_id}/reply-macros", tags=["reply-macros"])
logger = get_logger(__name__)


async def _get_account_or_404(account_id: str, db: AsyncSession):
    account = await account_crud.get_account(db, account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="계정을 찾을 수 없습니다.")
    return account


@router.get("", response_model=list[ReplyMacroRead])
async def list_macros(
    account_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    await require_account_tenant_access(account_id, db, identity)
    await _get_account_or_404(account_id, db)
    return await macro_crud.list_macros(db, account_id)


@router.post("", response_model=ReplyMacroRead, status_code=status.HTTP_201_CREATED)
async def create_macro(
    account_id: str,
    name: Annotated[str, Form()],
    target_chats: Annotated[str, Form(description="JSON array of chat/group ids")],
    message_content: Annotated[str, Form()],
    schedule_type: Annotated[str, Form()] = "interval",
    interval_hours: Annotated[int, Form()] = 24,
    fixed_time: Annotated[str | None, Form()] = None,
    max_sends_per_day: Annotated[int, Form()] = 10,
    is_active: Annotated[bool, Form()] = True,
    reply_to_message_id: Annotated[int | None, Form()] = None,
    file: Annotated[UploadFile | None, File()] = None,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    # NOTE: this endpoint accepts multipart/form-data ONLY, not a JSON body.
    # File uploads (UploadFile/File()) force the whole request into multipart
    # encoding, and FastAPI cannot deserialize a Pydantic model directly from
    # form fields — so every field must be its own Form(). Do not collapse
    # these back into a single `payload: ReplyMacroCreate` body param; that
    # combination makes FastAPI require a "payload" wrapper key that no real
    # client (JSON or multipart) can satisfy, and every create request 422s.
    await require_account_tenant_access(account_id, db, identity)
    await _get_account_or_404(account_id, db)

    try:
        target_chats_list = json.loads(target_chats)
    except json.JSONDecodeError:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="target_chats는 JSON 배열이어야 합니다.")

    try:
        payload = ReplyMacroCreate(
            name=name,
            target_chats=target_chats_list,
            message_content=message_content,
            schedule_type=schedule_type,
            interval_hours=interval_hours,
            fixed_time=fixed_time or None,
            max_sends_per_day=max_sends_per_day,
            is_active=is_active,
            reply_to_message_id=reply_to_message_id,
        )
    except ValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.errors())

    media_path = await save_broadcast_media(file) if file is not None else None
    macro = await macro_crud.create_macro(db, account_id, payload, media_path=media_path)
    logger.info("reply_macro_created", account_id=account_id, macro_id=macro.id)
    return macro


@router.get("/{macro_id}", response_model=ReplyMacroRead)
async def read_macro(
    account_id: str,
    macro_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    await require_account_tenant_access(account_id, db, identity)
    await _get_account_or_404(account_id, db)
    macro = await macro_crud.get_macro(db, macro_id)
    if macro is None or macro.account_id != account_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="답장매크로를 찾을 수 없습니다.")
    return macro


@router.put("/{macro_id}", response_model=ReplyMacroRead)
async def update_macro(
    account_id: str,
    macro_id: str,
    payload: ReplyMacroUpdate,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    await require_account_tenant_access(account_id, db, identity)
    await _get_account_or_404(account_id, db)
    macro = await macro_crud.get_macro(db, macro_id)
    if macro is None or macro.account_id != account_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="답장매크로를 찾을 수 없습니다.")
    return await macro_crud.update_macro(db, macro, payload)


@router.delete("/{macro_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_macro(
    account_id: str,
    macro_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    await require_account_tenant_access(account_id, db, identity)
    await _get_account_or_404(account_id, db)
    macro = await macro_crud.get_macro(db, macro_id)
    if macro is None or macro.account_id != account_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="답장매크로를 찾을 수 없습니다.")
    await macro_crud.delete_macro(db, macro)
    logger.info("reply_macro_deleted", account_id=account_id, macro_id=macro_id)


@router.post("/{macro_id}/execute", status_code=status.HTTP_202_ACCEPTED)
async def execute_macro_now(
    account_id: str,
    macro_id: str,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """Manually trigger a reply macro to execute immediately."""
    await require_account_tenant_access(account_id, db, identity)
    await _get_account_or_404(account_id, db)
    macro = await macro_crud.get_macro(db, macro_id)
    if macro is None or macro.account_id != account_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="답장매크로를 찾을 수 없습니다.")
    background_tasks.add_task(execute_reply_macro, macro.id)
    logger.info("reply_macro_triggered", account_id=account_id, macro_id=macro.id)
    return {"status": "accepted", "macro_id": macro.id}


@router.get("/{macro_id}/logs", response_model=list[ReplyMacroLogRead])
async def read_macro_logs(
    account_id: str,
    macro_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
    status_filter: str | None = None,
):
    await require_account_tenant_access(account_id, db, identity)
    await _get_account_or_404(account_id, db)
    macro = await macro_crud.get_macro(db, macro_id)
    if macro is None or macro.account_id != account_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="답장매크로를 찾을 수 없습니다.")
    return await macro_crud.list_logs(db, account_id, macro_id=macro_id, status=status_filter)