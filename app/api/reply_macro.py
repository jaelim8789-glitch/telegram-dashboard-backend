import json

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile, File, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_identity, Identity, require_account_tenant_access
from app.core.logging import get_logger
from app.crud import account as account_crud
from app.crud import reply_macro as macro_crud
from app.database import get_db
from app.schemas.reply_macro import ReplyMacroCreate, ReplyMacroRead, ReplyMacroLogRead
from app.services.media import save_broadcast_media
from app.services.random_reply_service import execute_random_reply

router = APIRouter(prefix="/api/accounts/{account_id}/reply-macros", tags=["reply-macros"])
logger = get_logger(__name__)


async def _get_account_or_404(account_id: str, db: AsyncSession):
    account = await account_crud.get_account(db, account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="계정을 찾을 수 없습니다.")
    return account


def _parse_target_chats(raw: str) -> list[str]:
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(c) for c in parsed]
    except (json.JSONDecodeError, TypeError):
        pass
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail="target_chats는 JSON 배열이어야 합니다.",
    )


@router.get("/toggle")
async def get_toggle_state(
    account_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """단순화된 랜덤 답장 on/off 상태 + 메시지 내용 조회 (계정당 1개, 대상 그룹은 자동)."""
    await require_account_tenant_access(account_id, db, identity)
    await _get_account_or_404(account_id, db)
    macro = await macro_crud.get_or_create_for_account(db, account_id)
    return {"is_active": macro.is_active, "message_content": macro.message_content}


@router.put("/toggle")
async def set_toggle_state(
    account_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """랜덤 답장 켜기/끄기 + 메시지 내용/대상 저장. 켜져있으면 스케줄러가 주기적으로 자동 실행."""
    await require_account_tenant_access(account_id, db, identity)
    await _get_account_or_404(account_id, db)
    macro = await macro_crud.get_or_create_for_account(db, account_id)

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="JSON body required.")

    is_active = bool(body.get("is_active", macro.is_active))
    message_content = body.get("message_content")
    target_chats_raw = body.get("target_chats")

    if is_active and not (message_content or macro.message_content):
        raise HTTPException(status_code=422, detail="메시지 내용을 입력해야 켤 수 있습니다.")

    macro.is_active = is_active
    if message_content is not None:
        macro.message_content = message_content
    if target_chats_raw is not None:
        if isinstance(target_chats_raw, list):
            macro.target_chats = json.dumps([str(c) for c in target_chats_raw])
        else:
            macro.target_chats = str(target_chats_raw)

    await db.commit()
    await db.refresh(macro)

    return {"is_active": macro.is_active, "message_content": macro.message_content, "target_chats": json.loads(macro.target_chats or "[]"), "last_sent_at": macro.last_sent_at.isoformat() if macro.last_sent_at else None}


@router.get("", response_model=list[ReplyMacroRead])
async def list_macros(
    account_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """계정의 답장 매크로 목록 조회."""
    await require_account_tenant_access(account_id, db, identity)
    await _get_account_or_404(account_id, db)
    return await macro_crud.list_macros(db, account_id)


@router.post("", response_model=ReplyMacroRead, status_code=status.HTTP_201_CREATED)
async def create_macro(
    account_id: str,
    name: str = Form("macro"),
    target_chats: str = Form("[]"),
    message_content: str = Form(""),
    is_active: bool = Form(True),
    file: UploadFile | None = File(default=None),
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """랜덤 답장 매크로 생성."""
    await require_account_tenant_access(account_id, db, identity)
    await _get_account_or_404(account_id, db)

    parsed_target_chats = _parse_target_chats(target_chats)
    if not parsed_target_chats:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="target_chats는 최소 1개 이상 필요합니다.")

    media_path = None
    if file is not None and file.filename:
        try:
            media_path = await save_broadcast_media(file)
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception(
                "reply_macro_attachment_save_failed",
                account_id=account_id,
                filename=getattr(file, "filename", None),
                error=str(exc),
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="첨부파일 저장 중 오류가 발생했습니다.",
            ) from exc

    macro = await macro_crud.create_macro(
        db,
        account_id,
        target_chats=parsed_target_chats,
        message_content=message_content,
        name=name,
        media_path=media_path,
        is_active=is_active,
    )
    logger.info("reply_macro_created", account_id=account_id, macro_id=macro.id)
    return macro


@router.post("/{macro_id}/random-reply")
async def execute_random_reply_endpoint(
    account_id: str,
    macro_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """랜덤 답장 실행: 대상 채팅방 최근 메시지 중 무작위 1명에게 Reply로 홍보글 전송 (중복 제외)."""
    await require_account_tenant_access(account_id, db, identity)
    await _get_account_or_404(account_id, db)
    macro = await macro_crud.get_macro(db, macro_id)
    if macro is None or macro.account_id != account_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="답장매크로를 찾을 수 없습니다.")
    result = await execute_random_reply(macro.id)
    logger.info("random_reply_executed", account_id=account_id, macro_id=macro.id, result=result)
    return result


@router.delete("/{macro_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_macro_endpoint(
    account_id: str,
    macro_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """답장매크로 삭제."""
    await require_account_tenant_access(account_id, db, identity)
    await _get_account_or_404(account_id, db)
    macro = await macro_crud.get_macro(db, macro_id)
    if macro is None or macro.account_id != account_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="답장매크로를 찾을 수 없습니다.")
    await macro_crud.delete_macro(db, macro)


@router.get("/{macro_id}/logs", response_model=list[ReplyMacroLogRead])
async def read_macro_logs(
    account_id: str,
    macro_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """이 매크로의 발송 로그 조회."""
    await require_account_tenant_access(account_id, db, identity)
    await _get_account_or_404(account_id, db)
    macro = await macro_crud.get_macro(db, macro_id)
    if macro is None or macro.account_id != account_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="답장매크로를 찾을 수 없습니다.")
    return await macro_crud.list_logs(db, account_id, macro_id=macro_id)


@router.get("/{macro_id}/used-targets")
async def read_used_targets(
    account_id: str,
    macro_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    """이 매크로에서 이미 답장한 대상 목록 조회."""
    await require_account_tenant_access(account_id, db, identity)
    await _get_account_or_404(account_id, db)
    macro = await macro_crud.get_macro(db, macro_id)
    if macro is None or macro.account_id != account_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="답장매크로를 찾을 수 없습니다.")
    return await macro_crud.get_used_targets(macro)