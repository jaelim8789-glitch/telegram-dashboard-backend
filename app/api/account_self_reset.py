"""Self-service reset for a phone number stuck on "이미 등록된 전화번호입니다".

When a user tries to register a Telegram automation account with a phone number
that already has a (usually stale/abandoned) `accounts` row — from an earlier
partial signup, a deleted-but-orphaned test, etc. — they hit a 409 with no way
to recover short of contacting an admin. This lets them prove they actually
control that phone number via the same Telegram code (+2FA) flow used for
normal account login, and only then clears the stale row so they can register
fresh.

Ownership proof, not tenant ownership, is what gates the delete here: the
requesting identity does NOT need to already own the stale `accounts` row
(that's the whole point — it's usually orphaned or belongs to an earlier,
abandoned attempt by the same person). What gates it is completing a real
Telegram login challenge for that phone, which nobody but the phone's actual
owner can do. Rate limiting keeps this from being used as an SMS-bombing or
brute-force vector against arbitrary numbers.
"""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request, status
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
)

from app.api.deps import Identity, get_current_identity
from app.core.logging import get_logger
from app.core.rate_limiter import check_rate_limit, get_client_ip, get_retry_after_seconds
from app.crud import account as account_crud
from app.database import get_db
from app.schemas.account_self_reset import (
    SelfResetResult,
    SelfResetSendCodeRequest,
    SelfResetVerify2FARequest,
    SelfResetVerifyCodeRequest,
)
from app.services.telethon_pool import pool

router = APIRouter(prefix="/api/accounts/self-reset", tags=["account-self-reset"])
logger = get_logger(__name__)

_DEAD_SESSION_ERRORS = (
    AuthKeyInvalidError,
    AuthKeyPermEmptyError,
    AuthKeyUnregisteredError,
    SessionExpiredError,
    SessionRevokedError,
)
_DEAD_SESSION_DETAIL = "인증 세션이 만료되었습니다. 처음부터(인증번호 요청) 다시 시도해주세요."

# send-code and verify-code/2fa share one budget per phone+category so a single
# phone number can't be hammered even across categories.
_SEND_CODE_LIMIT = dict(max_attempts=5, window_seconds=600.0)
_VERIFY_LIMIT = dict(max_attempts=10, window_seconds=600.0)


def _pool_key(phone: str) -> str:
    # Deliberately namespaced away from real account_ids (which are UUIDs) so a
    # temp reset session can never collide with — or be confused for — a pooled
    # client for an actual persisted Account row.
    return f"self-reset:{phone}"


def _config_error_to_http(exc: RuntimeError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))


async def _reset_phone(db: AsyncSession, phone: str) -> None:
    """Delete the stale accounts row (and everything FK-cascaded off it) for phone."""
    deleted = await account_crud.delete_account_by_phone(db, phone)
    logger.info("self_reset_account_deleted", phone=phone, had_existing_account=deleted is not None)


@router.post("/send-code", response_model=SelfResetResult)
async def self_reset_send_code(
    request: Request,
    payload: SelfResetSendCodeRequest,
    identity: Identity = Depends(get_current_identity),
):
    client_ip = get_client_ip(request)
    if not check_rate_limit(client_ip, "self_reset_send_code", **_SEND_CODE_LIMIT):
        retry_after = get_retry_after_seconds(client_ip, "self_reset_send_code", _SEND_CODE_LIMIT["window_seconds"])
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"요청이 너무 많습니다. {retry_after}초 후 다시 시도하세요.",
        )

    key = _pool_key(payload.phone)
    try:
        client = await pool.get_client(key, "", require_authorized=False)
    except RuntimeError as exc:
        raise _config_error_to_http(exc)

    try:
        sent = await asyncio.wait_for(client.send_code_request(payload.phone), timeout=30)
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
    except _DEAD_SESSION_ERRORS:
        await pool.remove_client(key)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_DEAD_SESSION_DETAIL)

    pool.set_pending_auth(key, sent.phone_code_hash)
    logger.info(
        "self_reset_code_sent",
        phone=payload.phone,
        requested_by_identity=identity.kind,
        requested_by_tenant=identity.tenant_id,
    )
    return SelfResetResult(reset=False, detail="인증번호를 전송했습니다.")


@router.post("/verify-code", response_model=SelfResetResult)
async def self_reset_verify_code(
    request: Request,
    payload: SelfResetVerifyCodeRequest,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    client_ip = get_client_ip(request)
    if not check_rate_limit(client_ip, "self_reset_verify", **_VERIFY_LIMIT):
        retry_after = get_retry_after_seconds(client_ip, "self_reset_verify", _VERIFY_LIMIT["window_seconds"])
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"요청이 너무 많습니다. {retry_after}초 후 다시 시도하세요.",
        )

    key = _pool_key(payload.phone)
    pending = pool.get_pending_auth(key)
    if pending is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="먼저 인증번호를 요청해주세요 (send-code).",
        )

    try:
        client = await pool.get_client(key, "", require_authorized=False)
    except RuntimeError as exc:
        raise _config_error_to_http(exc)

    try:
        await client.sign_in(phone=payload.phone, code=payload.code, phone_code_hash=pending.phone_code_hash)
    except SessionPasswordNeededError:
        return SelfResetResult(reset=False, requires_2fa=True, detail="2단계 인증 비밀번호가 필요합니다.")
    except PhoneCodeInvalidError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="인증번호가 올바르지 않습니다.")
    except PhoneCodeExpiredError:
        pool.clear_pending_auth(key)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="인증번호가 만료되었습니다. 다시 요청해주세요.")
    except FloodWaitError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"요청이 너무 많습니다. {exc.seconds}초 후 다시 시도하세요.",
        )
    except _DEAD_SESSION_ERRORS:
        await pool.remove_client(key)
        pool.clear_pending_auth(key)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_DEAD_SESSION_DETAIL)

    # Ownership proven — clear the stale row and tear down the throwaway client.
    await _reset_phone(db, payload.phone)
    pool.clear_pending_auth(key)
    await pool.remove_client(key)
    logger.info(
        "self_reset_completed",
        phone=payload.phone,
        stage="verify_code",
        requested_by_identity=identity.kind,
        requested_by_tenant=identity.tenant_id,
    )
    return SelfResetResult(reset=True, detail="본인 확인이 완료되어 기존 등록 정보를 초기화했습니다. 다시 등록해주세요.")


@router.post("/verify-2fa", response_model=SelfResetResult)
async def self_reset_verify_2fa(
    request: Request,
    payload: SelfResetVerify2FARequest,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    client_ip = get_client_ip(request)
    if not check_rate_limit(client_ip, "self_reset_verify", **_VERIFY_LIMIT):
        retry_after = get_retry_after_seconds(client_ip, "self_reset_verify", _VERIFY_LIMIT["window_seconds"])
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"요청이 너무 많습니다. {retry_after}초 후 다시 시도하세요.",
        )

    key = _pool_key(payload.phone)
    try:
        client = await pool.get_client(key, "", require_authorized=False)
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
        await pool.remove_client(key)
        pool.clear_pending_auth(key)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_DEAD_SESSION_DETAIL)

    await _reset_phone(db, payload.phone)
    pool.clear_pending_auth(key)
    await pool.remove_client(key)
    logger.info(
        "self_reset_completed",
        phone=payload.phone,
        stage="verify_2fa",
        requested_by_identity=identity.kind,
        requested_by_tenant=identity.tenant_id,
    )
    return SelfResetResult(reset=True, detail="본인 확인이 완료되어 기존 등록 정보를 초기화했습니다. 다시 등록해주세요.")
