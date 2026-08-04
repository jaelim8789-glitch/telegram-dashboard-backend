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
from app.realtime.handlers import register_account_realtime

router = APIRouter(prefix="/api/accounts", tags=["telegram-auth"])
logger = get_logger(__name__)


async def _get_account_or_404(account_id: str, db: AsyncSession) -> Account:
    account = await account_crud.get_account(db, account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="2    .")
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


_SENT_CODE_TYPE_TO_CHANNEL = {
    "SentCodeTypeApp": "telegram_app",
    "SentCodeTypeSms": "sms",
    "SentCodeTypeCall": "call",
    "SentCodeTypeFlashCall": "flash_call",
    "SentCodeTypeMissedCall": "flash_call",
}


def _sent_code_channel(sent) -> str | None:
    """Telethon's SentCode.type tells us whether the code went to the Telegram
    app, an SMS, or a phone call -- without this the UI can only guess, which
    is how 'the code never arrived' reports turn out to mean 'it arrived by
    SMS and the user only checked the Telegram app'."""
    type_name = type(sent.type).__name__ if getattr(sent, "type", None) is not None else None
    return _SENT_CODE_TYPE_TO_CHANNEL.get(type_name)


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

    session_string = ""
    if account.session_data:
        try:
            session_string = decrypt_session(account.session_data)
        except ValueError:
            await account_crud.set_auth_state(db, account, status="session_corrupted")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="세션 데이터가 손상되었습니다. 재인증이 필요합니다.",
            )
    try:
        # require_authorized=False: session_data here may be a pre-auth snapshot from
        # an earlier incomplete attempt (saved right after send_code_request, before
        # sign_in ever ran) — being unauthorized at this point is expected, not dead.
        client = await pool.get_client(account.id, session_string, require_authorized=False)
    except RuntimeError as exc:
        raise _config_error_to_http(exc)

    # flood_sleep_threshold=0 (see telethon_pool.py) makes Telethon raise
    # FloodWaitError immediately instead of silently sleeping through short
    # waits itself -- that's what fixed the 30s chat-action delay, but it
    # means a routine few-second flood wait during send-code now fails
    # outright instead of the client quietly absorbing it. Auto-retry once
    # for short waits so the user still gets their code; only surface a
    # 429 for waits long enough that blocking the request would be worse.
    _SHORT_FLOOD_WAIT_THRESHOLD_SECONDS = 10

    async def _request_code():
        return await asyncio.wait_for(client.send_code_request(account.phone), timeout=45)

    try:
        try:
            sent = await _request_code()
        except FloodWaitError as exc:
            if exc.seconds > _SHORT_FLOOD_WAIT_THRESHOLD_SECONDS:
                raise
            logger.info("send_code_short_flood_wait_retry", account_id=account.id, seconds=exc.seconds)
            await asyncio.sleep(exc.seconds)
            sent = await _request_code()
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
            detail=f"요청이 너무 많습니다. {exc.seconds}초 후 다시 시도해주세요.",
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

    # Delivery channel — Telegram picks this; the UI must tell the user where
    # to look (SMS vs Telegram app) or they'll think the code never arrived.
    channel = _sent_code_channel(sent)
    logger.info("verification_code_sent", account_id=account.id, channel=channel)
    return SendCodeResponse(sent=True, channel=channel, delivery_hint=_delivery_hint(channel))


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
        # Redis recovery is async — give it one chance before failing
        await asyncio.sleep(0.3)
        pending = pool.get_pending_auth(account.id)
    if pending is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="먼저 인증번호를 요청해주세요 (send-code).",
        )

    session_string = ""
    if account.session_data:
        try:
            session_string = decrypt_session(account.session_data)
        except ValueError:
            await account_crud.set_auth_state(db, account, status="session_corrupted")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="세션 데이터가 손상되었습니다. 재인증이 필요합니다.",
            )
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
        return AuthStepResult(status=account.status, detail=" .")
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
    me = await client.get_me()
    register_account_realtime(client, account.id, me.id if me else None)
    logger.info("account_authenticated", account_id=account.id, stage="verify_code")

    try:
        from app.services.telegram_actions import list_groups
        groups = await list_groups(account)
        account.group_count = len(groups)
        await db.commit()
    except Exception:
        pass

    try:
        from app.services.session_manager import SessionManager
        manager = SessionManager()
        if manager._initialized:
            await manager.connect(account.id, decrypt_session(account.session_data) if account.session_data else "")
    except Exception:
        pass

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

    session_string = ""
    if account.session_data:
        try:
            session_string = decrypt_session(account.session_data)
        except ValueError:
            await account_crud.set_auth_state(db, account, status="session_corrupted")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="세션 데이터가 손상되었습니다. 재인증이 필요합니다.",
            )
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
    me = await client.get_me()
    register_account_realtime(client, account.id, me.id if me else None)
    logger.info("account_authenticated", account_id=account.id, stage="verify_2fa")

    try:
        from app.services.telegram_actions import list_groups
        groups = await list_groups(account)
        account.group_count = len(groups)
        await db.commit()
    except Exception:
        pass

    try:
        from app.services.session_manager import SessionManager
        manager = SessionManager()
        if manager._initialized:
            await manager.connect(account.id, decrypt_session(account.session_data) if account.session_data else "")
    except Exception:
        pass

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
    except ValueError:
        await account_crud.set_auth_state(db, account, status="session_corrupted")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="세션 데이터가 손상되었습니다. 재인증이 필요합니다.",
        )

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


def _delivery_hint(channel: str | None) -> str | None:
    if channel == "sms":
        return "인증번호가 SMS 문자 메시지로 전송되었습니다. 휴대폰 문자를 확인하세요."
    if channel == "telegram_app":
        return "인증번호가 Telegram 앱 내 'Telegram' 서비스 메시지로 전송되었습니다. Telegram 앱을 확인하세요."
    if channel == "call":
        return "인증번호가 자동 전화로 안내됩니다. 걸려오는 전화를 받으세요."
    if channel == "flash_call":
        return "전화가 걸려오면 마지막 2자리로 인증합니다. 전화를 받으세요."
    return None