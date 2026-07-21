import hashlib
import hmac
import time

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from datetime import timedelta

from app.api.deps import Identity, get_current_identity
from app.config import settings
from app.core.logging import get_logger
from app.core.plans import get_plan
from app.core.rate_limiter import check_rate_limit, get_client_ip, get_retry_after_seconds
from app.core.time import utcnow_naive
from app.core.limits import (
    SEND_CODE_MAX_PER_IP,
    SEND_CODE_PER_IP_WINDOW,
    VERIFY_CODE_MAX_PER_IP,
    VERIFY_CODE_PER_IP_WINDOW,
)
from app.core.security import create_user_access_token, generate_otp_code, generate_user_api_key, hash_api_key, hash_password, mask_api_key
from app.crud import telegram_verification as verification_crud
from app.crud import api_key as api_key_crud
from app.crud import session as session_crud
from app.crud import user as user_crud
from app.database import get_db
from app.models.tenant import Tenant
from app.models.referral import ReferralCode
from app.models.user import User
from app.schemas.auth import (
    ForgotPasswordRequest,
    ForgotPasswordResponse,
    LinkTelegramRequest,
    LinkTelegramResponse,
    LoginWithApiKeyRequest,
    LoginWithApiKeyResponse,
    MeResponse,
    ResetPasswordRequest,
    ResetPasswordResponse,
    SendCodeRequest,
    SendCodeResponse,
    TelegramLoginRequest,
    TelegramLoginResponse,
    VerifyCodeRequest,
    VerifyCodeResponse,
)
from app.schemas.api_key import APIKeyLinkRequest, APIKeyRead
from app.services.sms_service import SmsSendError, send_verification_sms
from app.services.usage_tracker import apply_plan_limits

router = APIRouter(prefix="/api/auth", tags=["auth"])
logger = get_logger(__name__)


def _verify_telegram_widget(data: TelegramLoginRequest, bot_token: str) -> bool:
    raw = data.model_dump(exclude={"hash"})
    check_string_parts = []
    for key, value in sorted(raw.items()):
        if value is not None and value != "":
            check_string_parts.append(f"{key}={value}")
    check_string = "\n".join(check_string_parts)
    secret_key = hashlib.sha256(bot_token.encode()).digest()
    computed = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    return computed == data.hash


def _public_me_response(user: User, tenant: Tenant | None) -> MeResponse:
    return MeResponse(
        role="user",
        phone=user.phone,
        subscription_status=tenant.subscription_status if tenant else None,
        plan=tenant.plan if tenant else None,
        trial_expires_at=tenant.trial_expires_at if tenant else None,
        telegram_username=user.telegram_username or None,
        telegram_photo_url=user.telegram_photo_url or None,
        stars_balance=tenant.stars_balance if tenant else 0,
    )


@router.post("/send-code", response_model=SendCodeResponse)
async def send_code(payload: SendCodeRequest, request: Request, db: AsyncSession = Depends(get_db)):
    # Layer 1: per-IP rate limit
    client_ip = get_client_ip(request)
    if not check_rate_limit(
        client_ip, "send_code",
        max_attempts=SEND_CODE_MAX_PER_IP,
        window_seconds=SEND_CODE_PER_IP_WINDOW,
    ):
        retry_after = get_retry_after_seconds(
            client_ip, "send_code", window_seconds=SEND_CODE_PER_IP_WINDOW,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="너무 많은 요청이 발생했습니다. 잠시 후 다시 시도해주세요.",
            headers={"Retry-After": str(retry_after)},
        )

    # Layer 2: per-phone cooldown
    wait_seconds = await user_crud.seconds_until_next_code_allowed(db, payload.phone)
    if wait_seconds > 0:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"잠시 후 다시 시도해주세요 ({int(wait_seconds) + 1}초 후 재전송 가능).",
        )

    code = generate_otp_code()
    await user_crud.upsert_verification_code(db, payload.phone, code)
    try:
        await send_verification_sms(payload.phone, code)
    except SmsSendError as exc:
        logger.error("sms_send_failed", phone=payload.phone, error=str(exc))
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))

    logger.info("verification_code_sent", phone=payload.phone)
    return SendCodeResponse(sent=True)


@router.post("/verify-code", response_model=VerifyCodeResponse)
async def verify_code(payload: VerifyCodeRequest, request: Request, db: AsyncSession = Depends(get_db)):
    # Layer 1: per-IP rate limit
    client_ip = get_client_ip(request)
    if not check_rate_limit(
        client_ip, "verify_code",
        max_attempts=VERIFY_CODE_MAX_PER_IP,
        window_seconds=VERIFY_CODE_PER_IP_WINDOW,
    ):
        retry_after = get_retry_after_seconds(
            client_ip, "verify_code", window_seconds=VERIFY_CODE_PER_IP_WINDOW,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="너무 많은 요청이 발생했습니다. 잠시 후 다시 시도해주세요.",
            headers={"Retry-After": str(retry_after)},
        )

    # Layer 2: per-phone attempt limit
    if not await user_crud.verify_code(db, payload.phone, payload.code):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="인증번호가 올바르지 않거나 만료되었습니다.")

    from sqlalchemy import select
    result = await db.execute(select(Tenant).where(Tenant.phone == payload.phone))
    tenant = result.scalar_one_or_none()

    # A Tenant row can exist without ever having been through channel verification —
    # e.g. /api/payment/request-key (public, unauthenticated, pre-payment) creates a
    # "pending" Tenant stub for any phone on request. Only an already-*active*
    # tenant (paid & confirmed, or a previously-verified free trial) is genuinely
    # entitled to skip the gate; anything else (no tenant, or a pending stub) must
    # still prove channel membership like a brand-new signup.
    tenant_already_entitled = tenant is not None and tenant.subscription_status == "active"

    if not tenant_already_entitled and settings.telegram_official_channel_id:
        if payload.telegram_verification_token is None or not await verification_crud.consume_verified_token(
            db, payload.telegram_verification_token
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="공식 텔레그램 채널 가입 인증이 필요합니다. 채널 가입 후 다시 시도해주세요.",
            )

    user = await user_crud.get_or_create_user(db, payload.phone)
    raw_key = generate_user_api_key()
    await user_crud.set_api_key_hash(db, user, hash_api_key(raw_key))

    if not tenant:
        plan_def = get_plan("free")
        trial_hours = (plan_def["trial_days"] * 24) if plan_def else 72
        trial_expires = utcnow_naive() + timedelta(hours=trial_hours)
        referred_by = None
        if payload.referral_code:
            referred_by = await _resolve_referral_code(db, payload.referral_code, payload.phone)
        tenant = Tenant(
            phone=payload.phone,
            plan="free",
            subscription_status="active",
            trial_expires_at=trial_expires,
            referred_by=referred_by,
        )
        db.add(tenant)
        await db.flush()

        code = ReferralCode(code=tenant.referral_code, owner_id=tenant.id, is_active=True)
        db.add(code)

        if referred_by:
            referrer = await db.get(Tenant, referred_by)
            if referrer is not None:
                referrer.referral_code_uses = (referrer.referral_code_uses or 0) + 1

        await apply_plan_limits(db, tenant, "free")

    logger.info("user_api_key_issued", user_id=user.id)
    return VerifyCodeResponse(api_key=raw_key)


@router.post("/login-with-api-key", response_model=LoginWithApiKeyResponse)
async def login_with_api_key(payload: LoginWithApiKeyRequest, request: Request, db: AsyncSession = Depends(get_db)):
    client_ip = get_client_ip(request)
    if not check_rate_limit(client_ip, "api_key_login", max_attempts=20, window_seconds=300):
        retry_after = get_retry_after_seconds(client_ip, "api_key_login")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="너무 많은 로그인 시도가 있었습니다. 잠시 후 다시 시도해주세요.",
            headers={"Retry-After": str(retry_after)},
        )
    # Check User.api_key_hash first (for free-trial / self-service keys)
    user = await user_crud.get_by_api_key_hash(db, hash_api_key(payload.api_key))
    if user is not None and user.is_active:
        await user_crud.touch_last_login(db, user)
        tenant_id = await _resolve_tenant_id_by_user(db, user)
        raw_token, _ = await session_crud.create_session(
            db, user_id=user.id, tenant_id=tenant_id,
        )
        logger.info("user_login_success", user_id=user.id)
        return LoginWithApiKeyResponse(
            access_token=create_user_access_token(user.id),
            session_token=raw_token,
        )

    # Fallback: check ApiKeys table
    key_record = await api_key_crud.get_by_key(db, payload.api_key)
    if key_record is not None and key_record.is_active:
        raw_token, _ = await session_crud.create_session(
            db, api_key_id=key_record.id, tenant_id=key_record.tenant_id,
        )
        logger.info("api_key_login_success", api_key_id=key_record.id)
        return LoginWithApiKeyResponse(
            access_token=create_user_access_token(key_record.id),
            session_token=raw_token,
        )

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="유효하지 않거나 비활성화된 API 키입니다.")


@router.post("/telegram-login", response_model=TelegramLoginResponse)
async def telegram_login(
    payload: TelegramLoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    if not settings.telegram_bot_token:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Telegram Login이 설정되지 않았습니다.")

    client_ip = get_client_ip(request)
    if not check_rate_limit(client_ip, "telegram_login", max_attempts=10, window_seconds=300):
        retry_after = get_retry_after_seconds(client_ip, "telegram_login")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="너무 많은 로그인 시도가 있었습니다. 잠시 후 다시 시도해주세요.",
            headers={"Retry-After": str(retry_after)},
        )

    if not _verify_telegram_widget(payload, settings.telegram_bot_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Telegram 로그인 인증에 실패했습니다.")

    now_ts = int(time.time())
    if now_ts - payload.auth_date > 86400:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="인증이 만료되었습니다. 다시 시도해주세요.")

    user = await user_crud.get_or_create_user_by_telegram(
        db, payload.id, payload.username, payload.photo_url,
    )

    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="비활성화된 계정입니다.")

    await user_crud.touch_last_login(db, user)

    from sqlalchemy import select
    result = await db.execute(select(Tenant).where(Tenant.phone == user.phone))
    tenant = result.scalar_one_or_none()
    is_new_user = tenant is None

    if not tenant:
        plan_def = get_plan("free")
        trial_hours = (plan_def["trial_days"] * 24) if plan_def else 72
        trial_expires = utcnow_naive() + timedelta(hours=trial_hours)
        referred_by = None
        if payload.referral_code:
            referred_by = await _resolve_referral_code(db, payload.referral_code, user.phone)
        tenant = Tenant(
            phone=user.phone,
            plan="free",
            subscription_status="active",
            trial_expires_at=trial_expires,
            referred_by=referred_by,
        )
        db.add(tenant)
        await db.flush()

        code = ReferralCode(code=tenant.referral_code, owner_id=tenant.id, is_active=True)
        db.add(code)

        if referred_by:
            referrer = await db.get(Tenant, referred_by)
            if referrer is not None:
                referrer.referral_code_uses = (referrer.referral_code_uses or 0) + 1

        await apply_plan_limits(db, tenant, "free")

    raw_token, _ = await session_crud.create_session(
        db, user_id=user.id, tenant_id=tenant.id,
    )

    logger.info("telegram_login_success", user_id=user.id, telegram_id=payload.id)
    return TelegramLoginResponse(
        access_token=create_user_access_token(user.id),
        session_token=raw_token,
        is_new_user=is_new_user,
    )


@router.get("/me", response_model=MeResponse)
async def me(
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    if identity.kind == "user" and identity.user is not None:
        from sqlalchemy import select
        result = await db.execute(select(Tenant).where(Tenant.phone == identity.user.phone))
        tenant = result.scalar_one_or_none()
        return _public_me_response(identity.user, tenant)
    if identity.tenant_id:
        from sqlalchemy import select
        result = await db.execute(select(Tenant).where(Tenant.id == identity.tenant_id))
        tenant = result.scalar_one_or_none()
        if identity.user:
            return _public_me_response(identity.user, tenant)
        return MeResponse(
            role=identity.kind,
            subscription_status=tenant.subscription_status if tenant else None,
            plan=tenant.plan if tenant else None,
            trial_expires_at=tenant.trial_expires_at if tenant else None,
            stars_balance=tenant.stars_balance if tenant else 0,
        )
    return MeResponse(role=identity.kind)


async def _resolve_tenant_id_by_user(db: AsyncSession, user: User) -> str | None:
    from sqlalchemy import select
    from app.models.tenant import Tenant
    result = await db.execute(select(Tenant.id).where(Tenant.phone == user.phone))
    return result.scalar_one_or_none()


async def _resolve_referral_code(db: AsyncSession, code: str, new_user_phone: str) -> str | None:
    from sqlalchemy import select
    from app.models.referral import ReferralCode
    from app.models.tenant import Tenant

    if not code:
        return None

    result = await db.execute(select(ReferralCode).where(ReferralCode.code == code, ReferralCode.is_active == True))
    referral = result.scalar_one_or_none()
    if referral is None:
        return None

    referrer = await db.get(Tenant, referral.owner_id)
    if referrer is None:
        return None

    if referrer.phone == new_user_phone:
        return None

    return referrer.id


@router.post("/link-api-key", response_model=APIKeyRead)
async def link_api_key(
    payload: APIKeyLinkRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
):
    if identity.tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="이 기능에 접근할 수 없습니다. 먼저 결제/요금제를 설정해주세요.",
        )

    client_ip = get_client_ip(request)
    if not check_rate_limit(client_ip, "link_api_key", max_attempts=20, window_seconds=300):
        retry_after = get_retry_after_seconds(client_ip, "link_api_key", window_seconds=300)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="너무 많은 요청이 발생했습니다. 잠시 후 다시 시도해주세요.",
            headers={"Retry-After": str(retry_after)},
        )

    key = payload.key.strip()
    if not key.startswith("sk-") or len(key) < 35 or len(key) > 50:
        logger.warning("api_key_link_invalid_format", tenant_id=identity.tenant_id, client_ip=client_ip)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="올바르지 않은 API 키 형식입니다. 결제 후 발급받은 키를 입력해주세요.",
        )

    existing = await api_key_crud.get_by_key(db, key)
    if existing is not None:
        if existing.tenant_id == identity.tenant_id:
            logger.info("api_key_link_idempotent", api_key_id=existing.id, tenant_id=identity.tenant_id, client_ip=client_ip)
            return APIKeyRead(
                id=existing.id,
                masked_key=mask_api_key(existing.key),
                name=existing.name,
                is_active=existing.is_active,
                tenant_id=existing.tenant_id,
                created_at=existing.created_at,
                last_used=existing.last_used,
            )
        if existing.tenant_id is None:
            if getattr(existing, "purpose", None) == "admin_managed":
                logger.warning(
                    "api_key_link_admin_managed_rejected",
                    api_key_id=existing.id,
                    tenant_id=identity.tenant_id,
                    client_ip=client_ip,
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="이 API 키는 관리자 발급 키로, 사용자가 직접 연결할 수 없습니다. 관리자에게 문의해주세요.",
                )
            linked = await api_key_crud.link_api_key_to_tenant(db, key, identity.tenant_id)
            if linked is None:
                logger.warning("api_key_link_race", tenant_id=identity.tenant_id, client_ip=client_ip)
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="이 API 키는 이미 다른 테넌트에 연결되어 있습니다.",
                )
            logger.info("api_key_linked", api_key_id=linked.id, tenant_id=identity.tenant_id, client_ip=client_ip)
            return APIKeyRead(
                id=linked.id,
                masked_key=mask_api_key(linked.key),
                name=linked.name,
                is_active=linked.is_active,
                tenant_id=linked.tenant_id,
                created_at=linked.created_at,
                last_used=linked.last_used,
            )
        logger.warning(
            "api_key_link_conflict",
            api_key_id=existing.id,
            tenant_id=identity.tenant_id,
            existing_tenant_id=existing.tenant_id,
            client_ip=client_ip,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="이 API 키는 이미 다른 테넌트에 연결되어 있습니다.",
        )

    linked = await api_key_crud.link_api_key_to_tenant(db, key, identity.tenant_id)
    if linked is None:
        logger.warning("api_key_link_race", tenant_id=identity.tenant_id, client_ip=client_ip)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="이 API 키는 이미 다른 테넌트에 연결되어 있습니다.",
        )

    logger.info("api_key_linked", api_key_id=linked.id, tenant_id=identity.tenant_id, client_ip=client_ip)
    return APIKeyRead(
        id=linked.id,
        masked_key=mask_api_key(linked.key),
        name=linked.name,
        is_active=linked.is_active,
        tenant_id=linked.tenant_id,
        created_at=linked.created_at,
        last_used=linked.last_used,
    )


@router.get("/check-telegram/{telegram_id}")
async def check_telegram(
    telegram_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """봇에서 /start 시 웹 가입 여부를 조회하는 경량 엔드포인트.

    Returns:
        200: {linked: true, plan: str, phone: str} — 이미 가입된 사용자
        404: {linked: false} — 미가입 사용자
    """
    client_ip = get_client_ip(request)
    if not check_rate_limit(client_ip, "check_telegram", max_attempts=10, window_seconds=60):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="너무 많은 요청입니다")
    from sqlalchemy import select
    result = await db.execute(
        select(User).where(User.telegram_id == telegram_id)
    )
    user = result.scalar_one_or_none()
    if not user:
        return {"linked": False, "telegram_id": telegram_id}

    tenant_result = await db.execute(
        select(Tenant).where(Tenant.phone == user.phone)
    )
    tenant = tenant_result.scalar_one_or_none()

    return {
        "linked": True,
        "telegram_id": telegram_id,
        "phone": user.phone,
        "plan": tenant.plan if tenant else None,
        "subscription_status": tenant.subscription_status if tenant else None,
    }


@router.post("/link-telegram", response_model=LinkTelegramResponse)
async def link_telegram(
    payload: LinkTelegramRequest,
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
) -> LinkTelegramResponse:
    """웹 로그인된 사용자에게 Telegram ID를 연결하여 봇과 연동합니다.

    봇에서 /start 한 사용자의 telegram_id를 웹 계정에 연결합니다.
    이미 다른 웹 계정에 연결된 telegram_id면 409 충돌을 반환합니다.
    """
    if not identity.tenant_id:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다.")

    # Check if this telegram_id is already linked to another user
    from sqlalchemy import select
    existing = await db.execute(
        select(User).where(User.telegram_id == payload.telegram_id)
    )
    existing_user = existing.scalar_one_or_none()
    if existing_user and existing_user.id != identity.tenant_id:
        raise HTTPException(
            status_code=409,
            detail="이 Telegram 계정은 이미 다른 사용자와 연결되어 있습니다.",
        )

    # Link telegram_id to current user
    user = await db.get(User, identity.tenant_id)
    if not user:
        raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다.")

    user.telegram_id = payload.telegram_id
    if payload.telegram_username:
        user.telegram_username = payload.telegram_username
    if payload.telegram_photo_url:
        user.telegram_photo_url = payload.telegram_photo_url

    tenant = await db.get(Tenant, identity.tenant_id)
    await db.commit()

    logger.info(
        "telegram_linked",
        user_id=user.id,
        telegram_id=payload.telegram_id,
        username=payload.telegram_username,
    )

    return LinkTelegramResponse(
        linked=True,
        telegram_id=payload.telegram_id,
        plan=tenant.plan if tenant else None,
        subscription_status=tenant.subscription_status if tenant else None,
        message="Telegram 계정이 연결되었습니다. 이제 봇에서도 대시보드 정보를 확인할 수 있습니다.",
    )


@router.post("/logout")
async def logout(
    x_session_token: str | None = Header(default=None, alias="X-Session-Token"),
    db: AsyncSession = Depends(get_db),
):
    if x_session_token:
        session = await session_crud.get_session_by_token(db, x_session_token)
        if session is not None:
            await session_crud.deactivate_session(db, session)
    return {"ok": True}


@router.post("/forgot-password", response_model=ForgotPasswordResponse)
async def forgot_password(payload: ForgotPasswordRequest, request: Request, db: AsyncSession = Depends(get_db)):
    client_ip = get_client_ip(request)
    if not check_rate_limit(client_ip, "send_code", max_attempts=SEND_CODE_MAX_PER_IP, window_seconds=SEND_CODE_PER_IP_WINDOW):
        retry_after = get_retry_after_seconds(client_ip, "send_code", window_seconds=SEND_CODE_PER_IP_WINDOW)
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="너무 많은 요청이 발생했습니다. 잠시 후 다시 시도해주세요.", headers={"Retry-After": str(retry_after)})

    user = await user_crud.get_user_by_phone(db, payload.phone)
    if user is None:
        logger.info("forgot_password_no_user", phone=payload.phone)
        return ForgotPasswordResponse(message="인증 코드가 전송되었습니다")

    wait_seconds = await user_crud.seconds_until_next_code_allowed(db, payload.phone)
    if wait_seconds > 0:
        return ForgotPasswordResponse(message="인증 코드가 전송되었습니다")

    code = generate_otp_code()
    await user_crud.upsert_verification_code(db, payload.phone, code)

    if user.telegram_id:
        from app.services.telegram_notify import send_telegram_message
        await send_telegram_message(user.telegram_id, f"TeleMon 비밀번호 재설정 인증 코드: {code}")

    logger.info("forgot_password_code_sent", phone=payload.phone)
    return ForgotPasswordResponse(message="인증 코드가 전송되었습니다")


@router.post("/reset-password", response_model=ResetPasswordResponse)
async def reset_password(payload: ResetPasswordRequest, request: Request, db: AsyncSession = Depends(get_db)):
    if not await user_crud.verify_code(db, payload.phone, payload.code):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="인증번호가 올바르지 않거나 만료되었습니다.")

    user = await user_crud.get_user_by_phone(db, payload.phone)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="사용자를 찾을 수 없습니다.")

    user.password_hash = hash_password(payload.new_password)
    await db.commit()

    logger.info("password_reset", user_id=user.id)
    return ResetPasswordResponse(message="비밀번호가 재설정되었습니다")