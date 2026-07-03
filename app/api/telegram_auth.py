from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
    UserDeactivatedBanError,
    UserDeactivatedError,
)

from app.core.crypto import decrypt_session, encrypt_session
from app.core.logging import get_logger
from app.crud import account as account_crud
from app.database import get_db
from app.models.account import Account
from app.schemas.telegram_auth import AuthStepResult, SendCodeResponse, Verify2FARequest, VerifyCodeRequest
from app.services.telethon_pool import pool

router = APIRouter(prefix="/api/accounts", tags=["telegram-auth"])
logger = get_logger(__name__)


async def _get_account_or_404(account_id: str, db: AsyncSession) -> Account:
    account = await account_crud.get_account(db, account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="계정을 찾을 수 없습니다.")
    return account


def _config_error_to_http(exc: RuntimeError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))


@router.post("/{account_id}/send-code", response_model=SendCodeResponse)
async def send_code(account_id: str, db: AsyncSession = Depends(get_db)):
    account = await _get_account_or_404(account_id, db)

    try:
        client = await pool.get_client(account.id)
    except RuntimeError as exc:
        raise _config_error_to_http(exc)

    try:
        sent = await client.send_code_request(account.phone)
    except PhoneNumberInvalidError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="유효하지 않은 전화번호입니다.")
    except FloodWaitError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"요청이 너무 많습니다. {exc.seconds}초 후 다시 시도하세요.",
        )
    except UserDeactivatedBanError:
        await account_crud.set_auth_state(db, account, status="banned")
        logger.warning("account_banned", account_id=account.id, stage="send_code")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="차단된 계정입니다.")

    pool.set_pending_auth(account.id, sent.phone_code_hash)
    logger.info("verification_code_sent", account_id=account.id)
    return SendCodeResponse(sent=True)


@router.post("/{account_id}/verify-code", response_model=AuthStepResult)
async def verify_code(account_id: str, payload: VerifyCodeRequest, db: AsyncSession = Depends(get_db)):
    account = await _get_account_or_404(account_id, db)

    pending = pool.get_pending_auth(account.id)
    if pending is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="먼저 인증번호를 요청해주세요 (send-code).",
        )

    try:
        client = await pool.get_client(account.id)
    except RuntimeError as exc:
        raise _config_error_to_http(exc)

    try:
        await client.sign_in(phone=account.phone, code=payload.code, phone_code_hash=pending.phone_code_hash)
    except SessionPasswordNeededError:
        return AuthStepResult(status=account.status, requires_2fa=True, detail="2단계 인증 비밀번호가 필요합니다.")
    except PhoneCodeInvalidError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="인증번호가 올바르지 않습니다.")
    except PhoneCodeExpiredError:
        pool.clear_pending_auth(account.id)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="인증번호가 만료되었습니다. 다시 요청해주세요.",
        )
    except FloodWaitError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"요청이 너무 많습니다. {exc.seconds}초 후 다시 시도하세요.",
        )

    session_string = client.session.save()
    account = await account_crud.set_auth_state(
        db, account, status="active", session_data=encrypt_session(session_string)
    )
    pool.clear_pending_auth(account.id)
    logger.info("account_authenticated", account_id=account.id, stage="verify_code")
    return AuthStepResult(status=account.status, requires_2fa=False)


@router.post("/{account_id}/verify-2fa", response_model=AuthStepResult)
async def verify_2fa(account_id: str, payload: Verify2FARequest, db: AsyncSession = Depends(get_db)):
    account = await _get_account_or_404(account_id, db)

    try:
        client = await pool.get_client(account.id)
    except RuntimeError as exc:
        raise _config_error_to_http(exc)

    try:
        await client.sign_in(password=payload.password)
    except PasswordHashInvalidError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="2단계 인증 비밀번호가 올바르지 않습니다.")
    except FloodWaitError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"요청이 너무 많습니다. {exc.seconds}초 후 다시 시도하세요.",
        )

    session_string = client.session.save()
    account = await account_crud.set_auth_state(
        db, account, status="active", session_data=encrypt_session(session_string)
    )
    pool.clear_pending_auth(account.id)
    logger.info("account_authenticated", account_id=account.id, stage="verify_2fa")
    return AuthStepResult(status=account.status, requires_2fa=False)


@router.get("/{account_id}/status", response_model=AuthStepResult)
async def get_status(account_id: str, db: AsyncSession = Depends(get_db)):
    account = await _get_account_or_404(account_id, db)

    if not account.session_data:
        return AuthStepResult(status=account.status, detail="아직 인증되지 않은 계정입니다.")

    try:
        session_string = decrypt_session(account.session_data)
        client = await pool.get_client(account.id, session_string)
    except RuntimeError as exc:
        raise _config_error_to_http(exc)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))

    try:
        authorized = await client.is_user_authorized()
    except (UserDeactivatedBanError, UserDeactivatedError):
        account = await account_crud.set_auth_state(db, account, status="banned", touch_activity=False)
        logger.warning("account_banned", account_id=account.id, stage="status_check")
        return AuthStepResult(status=account.status, detail="계정이 차단되었습니다.")

    new_status = "active" if authorized else "inactive"
    if new_status != account.status:
        account = await account_crud.set_auth_state(db, account, status=new_status, touch_activity=authorized)

    return AuthStepResult(status=account.status)
