import asyncio

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from telethon.errors import (
    AuthKeyInvalidError,
    AuthKeyPermEmptyError,
    AuthKeyUnregisteredError,
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionExpiredError,
    SessionPasswordNeededError,
    SessionRevokedError,
    UserDeactivatedBanError,
    UserDeactivatedError,
)

from app.api.deps import get_current_identity, Identity, require_account_tenant_access
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


# The auth_key backing an in-progress login is dead beyond recovery — most commonly
# because the process restarted (wiping TelethonClientPool's in-memory clients)
# between two steps of send-code -> verify-code -> verify-2fa and no persisted
# session existed yet to reconnect with. There is no way to resume from here; the
# only path forward is to start over from send-code.
_DEAD_SESSION_ERRORS = (
    AuthKeyInvalidError,
    AuthKeyPermEmptyError,
    AuthKeyUnregisteredError,
    SessionExpiredError,
    SessionRevokedError,
)

_DEAD_SESSION_DETAIL = "인증 세션이 만료되었습니다. 처음부터(인증번호 요청) 다시 시도해주세요."


async def _recover_from_dead_session(account_id: str, db: AsyncSession, account: Account) -> None:
    """Self-heal so the next attempt starts clean instead of reusing a poisoned
    in-memory client or a stale session string."""
    await pool.remove_client(account_id)
    await account_crud.mark_account_session_invalid(db, account)


@router.post("/{account_id}/send-code", response_model=SendCodeResponse)
async def send_code(
    account_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    await require_account_tenant_access(account_id, db, identity)
    account = await _get_account_or_404(account_id, db)

    session_string = decrypt_session(account.session_data) if account.session_data else ""
    try:
        # require_authorized=False: session_data here may be a pre-auth snapshot from
        # an earlier incomplete attempt (saved right after send_code_request, before
        # sign_in ever ran) — being unauthorized at this point is expected, not dead.
        client = await pool.get_client(account.id, session_string, require_authorized=False)
    except RuntimeError as exc:
        raise _config_error_to_http(exc)

    try:
        # Add timeout wrapper to avoid hanging on slow Telegram SMS delivery
        sent = await asyncio.wait_for(client.send_code_request(account.phone), timeout=30)
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="텔레그램 서버 응답이 지연되고 있습니다. 잠시 후 다시 시도해주세요.",
        )
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
    except _DEAD_SESSION_ERRORS:
        # A previously-persisted session_data turned out to be dead (e.g. revoked
        # from the Telegram side). Clear it so the retry this error message asks
        # for actually starts from a blank client instead of the same dead one.
        await _recover_from_dead_session(account_id, db, account)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_DEAD_SESSION_DETAIL)

    # Persist the connection's auth_key immediately — if the process restarts
    # before verify-code, the next request reconnects with this session instead
    # of a blank one.
    await account_crud.save_session_snapshot(db, account, encrypt_session(client.session.save()))

    pool.set_pending_auth(account.id, sent.phone_code_hash)
    logger.info("verification_code_sent", account_id=account.id)
    return SendCodeResponse(sent=True)


@router.post("/{account_id}/verify-code", response_model=AuthStepResult)
async def verify_code(
    account_id: str,
    payload: VerifyCodeRequest,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    await require_account_tenant_access(account_id, db, identity)
    account = await _get_account_or_404(account_id, db)

    pending = pool.get_pending_auth(account.id)
    if pending is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="먼저 인증번호를 요청해주세요 (send-code).",
        )

    session_string = decrypt_session(account.session_data) if account.session_data else ""
    try:
        # require_authorized=False: this session was just saved by send_code before
        # sign_in ever ran — "not yet authorized" is the expected state here, not a
        # dead session. (This was the actual bug behind the 500s: the pool treated
        # every pre-auth session as dead and raised SessionInvalidError uncaught.)
        client = await pool.get_client(account.id, session_string, require_authorized=False)
    except RuntimeError as exc:
        raise _config_error_to_http(exc)

    try:
        await client.sign_in(phone=account.phone, code=payload.code, phone_code_hash=pending.phone_code_hash)
    except SessionPasswordNeededError:
        # The auth_key is now fully established even though the user still has to
        # complete 2FA — persist it so a restart before verify-2fa can resume here
        # instead of stranding the account with a blank client.
        await account_crud.save_session_snapshot(db, account, encrypt_session(client.session.save()))
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
    except _DEAD_SESSION_ERRORS:
        await _recover_from_dead_session(account_id, db, account)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_DEAD_SESSION_DETAIL)

    session_string = client.session.save()
    account = await account_crud.set_auth_state(
        db, account, status="active", session_data=encrypt_session(session_string)
    )
    pool.clear_pending_auth(account.id)
    logger.info("account_authenticated", account_id=account.id, stage="verify_code")
    return AuthStepResult(status=account.status, requires_2fa=False)


@router.post("/{account_id}/verify-2fa", response_model=AuthStepResult)
async def verify_2fa(
    account_id: str,
    payload: Verify2FARequest,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    await require_account_tenant_access(account_id, db, identity)
    account = await _get_account_or_404(account_id, db)

    session_string = decrypt_session(account.session_data) if account.session_data else ""
    try:
        # require_authorized=False — same reasoning as verify_code: sign_in(password=...)
        # is what completes authorization, so it's expected to be unauthorized here.
        client = await pool.get_client(account.id, session_string, require_authorized=False)
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
    except _DEAD_SESSION_ERRORS:
        await _recover_from_dead_session(account_id, db, account)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_DEAD_SESSION_DETAIL)

    session_string = client.session.save()
    account = await account_crud.set_auth_state(
        db, account, status="active", session_data=encrypt_session(session_string)
    )
    pool.clear_pending_auth(account.id)
    logger.info("account_authenticated", account_id=account.id, stage="verify_2fa")
    return AuthStepResult(status=account.status, requires_2fa=False)


@router.get("/{account_id}/status", response_model=AuthStepResult)
async def get_status(
    account_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    await require_account_tenant_access(account_id, db, identity)
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