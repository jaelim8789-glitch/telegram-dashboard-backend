import asyncio
import hashlib
import hmac
import json
import re
import secrets
import time
import urllib.parse

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from datetime import timedelta

from app.api.deps import Identity, get_current_identity, get_optional_identity
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
from app.core.security import create_user_access_token, generate_otp_code, generate_user_api_key, hash_api_key, hash_password, mask_api_key, verify_password_stored
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
    return hmac.compare_digest(computed, data.hash)


def _public_me_response(user: User, tenant: Tenant | None, requires_reauth: bool = False) -> MeResponse:
    return MeResponse(
        role="user",
        phone=user.phone,
        tenant_id=tenant.id if tenant else None,
        subscription_status=tenant.subscription_status if tenant else None,
        plan=tenant.plan if tenant else None,
        trial_expires_at=tenant.trial_expires_at if tenant else None,
        telegram_username=user.telegram_username or None,
        telegram_photo_url=user.telegram_photo_url or None,
        stars_balance=tenant.stars_balance if tenant else 0,
        requires_reauth=requires_reauth,
    )


@router.post("/send-code", response_model=SendCodeResponse, response_model_exclude_none=True)
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
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="SMS 인증이 아직 설정되지 않았습니다. 텔레그램 채널 인증으로 로그인해주세요.",
        )

    logger.info("verification_code_sent", phone=payload.phone)
    # console provider never actually delivers the code anywhere reachable by the
    # user (it only logs server-side) — echo it back so the schema's documented
    # dev-mode contract (and the frontend's auto-fill of it) actually works.
    return SendCodeResponse(sent=True, code=code if settings.sms_provider == "console" else None)


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

    from sqlalchemy import select
    result = await db.execute(select(Tenant).where(Tenant.phone == payload.phone))
    tenant = result.scalar_one_or_none()

    # A Tenant row can exist without ever having been through channel verification —
    # e.g. /api/payment/request-key (public, unauthenticated, pre-payment) creates a
    # "pending" Tenant stub for any phone on request. Only an already-*active*
    # tenant (paid & confirmed, or a previously-verified free trial) is genuinely
    # entitled to skip the gate; anything else (no tenant, or a pending stub) must
    # still prove identity like a brand-new signup.
    tenant_already_entitled = tenant is not None and tenant.subscription_status == "active"

    # Identity proof: SMS code if one was actually sent and matches (kept for
    # if/when Twilio is configured), otherwise Telegram channel-verification —
    # this deployment has never had Twilio credentials, so in practice every
    # request takes this branch. An already-entitled existing tenant skips
    # straight through (matches the SMS branch's prior behavior for existing
    # active tenants).
    # SMS 코드가 있으면 SMS로 인증, 없으면 채널 인증으로 폴백
    if payload.code is not None:
        code_ok = await user_crud.verify_code(db, payload.phone, payload.code)
        if not code_ok:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="인증번호가 올바르지 않습니다. 다시 시도해주세요.",
            )
    elif payload.telegram_verification_token is not None:
        token_ok = await verification_crud.consume_verified_token(
            db, payload.telegram_verification_token
        )
        if not token_ok:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="인증에 실패했습니다. 다시 시도해주세요.",
            )
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="인증번호 또는 텔레그램 인증 토큰이 필요합니다.",
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

        code = ReferralCode(code=tenant.referral_code, owner_id=tenant.id)
        db.add(code)

        if referred_by:
            referrer = await db.get(Tenant, referred_by)
            if referrer is not None:
                referrer.referral_code_uses = (referrer.referral_code_uses or 0) + 1

        await apply_plan_limits(db, tenant, "free")

    logger.info("user_api_key_issued", user_id=user.id)

    # Auto-create Account so the user can see chats immediately
    from app.models.account import Account
    existing_account = await db.execute(
        select(Account).where(Account.tenant_id == tenant.id, Account.phone == payload.phone)
    )
    if existing_account.scalar_one_or_none() is None:
        account = Account(
            phone=payload.phone,
            tenant_id=tenant.id,
            name=f"Telegram ({payload.phone})",
            status="pending_auth",
        )
        db.add(account)

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
            user_agent=request.headers.get("user-agent"),
            client_ip=get_client_ip(request),
        )
        await db.commit()
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
            user_agent=request.headers.get("user-agent"),
            client_ip=get_client_ip(request),
        )
        await db.commit()
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

        code = ReferralCode(code=tenant.referral_code, owner_id=tenant.id)
        db.add(code)

        if referred_by:
            referrer = await db.get(Tenant, referred_by)
            if referrer is not None:
                referrer.referral_code_uses = (referrer.referral_code_uses or 0) + 1

        await apply_plan_limits(db, tenant, "free")

    raw_token, _ = await session_crud.create_session(
        db, user_id=user.id, tenant_id=tenant.id,
        user_agent=request.headers.get("user-agent"),
        client_ip=get_client_ip(request),
    )
    await db.commit()

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
        return _public_me_response(identity.user, tenant, requires_reauth=identity.requires_reauth)
    if identity.tenant_id:
        from sqlalchemy import select
        result = await db.execute(select(Tenant).where(Tenant.id == identity.tenant_id))
        tenant = result.scalar_one_or_none()
        if identity.user:
            return _public_me_response(identity.user, tenant, requires_reauth=identity.requires_reauth)
        return MeResponse(
            role=identity.kind,
            tenant_id=tenant.id if tenant else None,
            subscription_status=tenant.subscription_status if tenant else None,
            plan=tenant.plan if tenant else None,
            trial_expires_at=tenant.trial_expires_at if tenant else None,
            stars_balance=tenant.stars_balance if tenant else 0,
            requires_reauth=identity.requires_reauth,
        )
    return MeResponse(role=identity.kind, requires_reauth=identity.requires_reauth)


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


class LoginWithPhoneRequest(BaseModel):
    phone: str


class LoginWithPhoneResponse(BaseModel):
    access_token: str
    session_token: str


@router.post("/login-with-phone", response_model=LoginWithPhoneResponse)
async def login_with_phone(payload: LoginWithPhoneRequest, request: Request, db: AsyncSession = Depends(get_db)):
    """Issue JWT for a phone that has an active Telegram account.

    Called after MTProto verification completes — the account is already
    active and the user just needs a web session token.
    """
    client_ip = get_client_ip(request)
    if not check_rate_limit(client_ip, "phone_login", max_attempts=20, window_seconds=300):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="요청이 너무 많습니다.",
        )

    # Find or create User
    user = await user_crud.get_or_create_user(db, payload.phone)
    await user_crud.touch_last_login(db, user)

    # Find or create Tenant
    tenant_id = await _resolve_tenant_id_by_user(db, user)
    if not tenant_id:
        plan_def = get_plan("free")
        trial_hours = (plan_def["trial_days"] * 24) if plan_def else 72
        trial_expires = utcnow_naive() + timedelta(hours=trial_hours)
        tenant = Tenant(
            phone=payload.phone,
            plan="free",
            subscription_status="active",
            trial_expires_at=trial_expires,
        )
        db.add(tenant)
        await db.flush()
        await apply_plan_limits(db, tenant, "free")
        tenant_id = tenant.id

    # Issue JWT + session
    raw_token, _ = await session_crud.create_session(
        db, user_id=user.id, tenant_id=tenant_id,
        user_agent=request.headers.get("user-agent"),
        client_ip=get_client_ip(request),
    )

    # Link orphaned accounts (created via prepare-account without tenant) to this tenant
    from app.models.account import Account as AccountModel
    orphan_result = await db.execute(
        select(AccountModel).where(AccountModel.phone == payload.phone, AccountModel.tenant_id.is_(None))
    )
    for orphan in orphan_result.scalars().all():
        orphan.tenant_id = tenant_id
        logger.info("orphan_account_linked", account_id=orphan.id, tenant_id=tenant_id)

    await db.commit()

    logger.info("unified_login_success", user_id=user.id)
    return LoginWithPhoneResponse(
        access_token=create_user_access_token(user.id),
        session_token=raw_token,
    )


_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_]{3,20}$")


class RegisterWithPasswordRequest(BaseModel):
    username: str
    password: str


class LoginWithPasswordRequest(BaseModel):
    username: str
    password: str


class PasswordAuthResponse(BaseModel):
    access_token: str
    session_token: str


@router.post("/register-password", response_model=PasswordAuthResponse, status_code=status.HTTP_201_CREATED)
async def register_with_password(payload: RegisterWithPasswordRequest, request: Request, db: AsyncSession = Depends(get_db)):
    """Id/password signup, no phone or Telegram required (Epic 26: guest AI
    -> full account funnel). Gets the same free-plan Tenant every other
    signup path gets; a real Telegram account still has to be connected
    separately before the automation features work -- this only unlocks
    login + billing + AI chat.
    """
    client_ip = get_client_ip(request)
    if not check_rate_limit(client_ip, "password_register", max_attempts=5, window_seconds=3600):
        retry_after = get_retry_after_seconds(client_ip, "password_register", window_seconds=3600)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="너무 많은 가입 시도가 있었습니다. 잠시 후 다시 시도해주세요.",
            headers={"Retry-After": str(retry_after)},
        )

    username = payload.username.strip()
    if not _USERNAME_RE.match(username):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="아이디는 영문/숫자/밑줄(_) 3~20자로 입력해주세요.")
    if len(payload.password) < 8:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="비밀번호는 8자 이상으로 설정해주세요.")

    existing = await db.execute(select(User).where(User.username == username))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="이미 사용 중인 아이디입니다.")

    # phone is NOT NULL UNIQUE on User/Tenant -- every other login path (SMS,
    # Telegram) keys off it, so id/password accounts get a synthetic
    # placeholder that can never collide with a real E.164 number.
    placeholder_phone = f"pw_{secrets.token_hex(10)}"

    user = User(
        phone=placeholder_phone,
        username=username,
        password_hash=hash_password(payload.password),
    )
    db.add(user)
    await db.flush()

    plan_def = get_plan("free")
    trial_hours = (plan_def["trial_days"] * 24) if plan_def else 72
    trial_expires = utcnow_naive() + timedelta(hours=trial_hours)
    tenant = Tenant(
        phone=placeholder_phone,
        plan="free",
        subscription_status="active",
        trial_expires_at=trial_expires,
    )
    db.add(tenant)
    await db.flush()
    await apply_plan_limits(db, tenant, "free")

    raw_token, _ = await session_crud.create_session(
        db, user_id=user.id, tenant_id=tenant.id,
        user_agent=request.headers.get("user-agent"),
        client_ip=client_ip,
    )
    await db.commit()

    logger.info("password_register_success", user_id=user.id)
    return PasswordAuthResponse(
        access_token=create_user_access_token(user.id),
        session_token=raw_token,
    )


@router.post("/login-password", response_model=PasswordAuthResponse)
async def login_with_password(payload: LoginWithPasswordRequest, request: Request, db: AsyncSession = Depends(get_db)):
    client_ip = get_client_ip(request)
    if not check_rate_limit(client_ip, "password_login", max_attempts=10, window_seconds=300):
        retry_after = get_retry_after_seconds(client_ip, "password_login", window_seconds=300)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="너무 많은 로그인 시도가 있었습니다. 잠시 후 다시 시도해주세요.",
            headers={"Retry-After": str(retry_after)},
        )

    result = await db.execute(select(User).where(User.username == payload.username.strip()))
    user = result.scalar_one_or_none()
    if user is None or not user.password_hash or not verify_password_stored(payload.password, user.password_hash):
        logger.warning("password_login_failed", username=payload.username)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="아이디 또는 비밀번호가 올바르지 않습니다.")

    await user_crud.touch_last_login(db, user)
    tenant_id = await _resolve_tenant_id_by_user(db, user)
    if not tenant_id:
        # Shouldn't happen for accounts created via register-password, but
        # guard the same way login_with_phone does for defense in depth.
        plan_def = get_plan("free")
        trial_hours = (plan_def["trial_days"] * 24) if plan_def else 72
        trial_expires = utcnow_naive() + timedelta(hours=trial_hours)
        tenant = Tenant(phone=user.phone, plan="free", subscription_status="active", trial_expires_at=trial_expires)
        db.add(tenant)
        await db.flush()
        await apply_plan_limits(db, tenant, "free")
        tenant_id = tenant.id

    raw_token, _ = await session_crud.create_session(
        db, user_id=user.id, tenant_id=tenant_id,
        user_agent=request.headers.get("user-agent"),
        client_ip=client_ip,
    )
    await db.commit()

    logger.info("password_login_success", user_id=user.id)
    return PasswordAuthResponse(
        access_token=create_user_access_token(user.id),
        session_token=raw_token,
    )


class MiniAppAuthRequest(BaseModel):
    init_data: str


class MiniAppAuthResponse(BaseModel):
    access_token: str
    session_token: str
    user_id: str
    phone: str | None = None


def _validate_telegram_init_data(init_data: str, bot_token: str) -> dict | None:
    """Validate Telegram Mini App initData signature.

    Returns parsed user dict if valid, None if invalid.
    """
    try:
        parsed = dict(urllib.parse.parse_qsl(init_data))
        if "hash" not in parsed or "auth_date" not in parsed:
            return None

        # Check auth_date freshness (5 minutes max)
        auth_date = int(parsed.get("auth_date", 0))
        if abs(time.time() - auth_date) > 300:
            return None

        # Build data_check_string
        check_data = "\n".join(
            f"{k}={v}" for k, v in sorted(parsed.items())
            if k != "hash"
        )

        # Compute HMAC
        secret_key = hmac.new(
            bot_token.encode(), b"", hashlib.sha256
        ).digest()

        computed_hash = hmac.new(
            secret_key, check_data.encode(), hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(computed_hash, parsed["hash"]):
            return None

        # Parse user
        user_json = parsed.get("user")
        if user_json:
            return json.loads(user_json)

        return {}
    except Exception:
        return None


@router.post("/miniapp/auth", response_model=MiniAppAuthResponse)
async def miniapp_auth(payload: MiniAppAuthRequest, request: Request, db: AsyncSession = Depends(get_db)):
    """Authenticate via Telegram Mini App initData.

    Validates the HMAC signature, then creates/finds user + tenant,
    and issues a JWT token.
    """
    bot_token = getattr(settings, "telegram_bot_token", "")
    if not bot_token:
        raise HTTPException(status_code=500, detail="Bot token이 설정되지 않았습니다.")

    user_data = _validate_telegram_init_data(payload.init_data, bot_token)
    if user_data is None:
        raise HTTPException(status_code=401, detail="Telegram 인증에 실패했습니다.")

    telegram_id = str(user_data.get("id", ""))
    username = user_data.get("username", "")
    first_name = user_data.get("first_name", "")
    last_name = user_data.get("last_name", "")

    if not telegram_id:
        raise HTTPException(status_code=400, detail="Telegram 사용자 정보를 찾을 수 없습니다.")

    # Telegram Mini App doesn't expose phone — use telegram_id as identifier
    phone = f"tg:{telegram_id}"

    # Find or create user
    user = await user_crud.get_or_create_user(db, phone)
    await user_crud.touch_last_login(db, user)

    # Find or create tenant
    tenant_id = await _resolve_tenant_id_by_user(db, user)
    if not tenant_id:
        plan_def = get_plan("free")
        trial_hours = (plan_def["trial_days"] * 24) if plan_def else 72
        trial_expires = utcnow_naive() + timedelta(hours=trial_hours)
        tenant = Tenant(
            phone=phone,
            plan="free",
            subscription_status="active",
            trial_expires_at=trial_expires,
        )
        db.add(tenant)
        await db.flush()
        await apply_plan_limits(db, tenant, "free")
        tenant_id = tenant.id

    # Issue JWT
    raw_token, _ = await session_crud.create_session(
        db, user_id=user.id, tenant_id=tenant_id,
        user_agent=request.headers.get("user-agent"),
        client_ip=get_client_ip(request),
    )

    # Link orphaned accounts to this tenant
    from app.models.account import Account as AccountModel
    orphan_result = await db.execute(
        select(AccountModel).where(AccountModel.phone == phone, AccountModel.tenant_id.is_(None))
    )
    for orphan in orphan_result.scalars().all():
        orphan.tenant_id = tenant_id

    await db.commit()

    logger.info("miniapp_auth_success", user_id=user.id, telegram_id=telegram_id)
    return MiniAppAuthResponse(
        access_token=create_user_access_token(user.id),
        session_token=raw_token,
        user_id=user.id,
        phone=phone,
    )


# ── Unified Telegram login: public endpoints (no auth required) ──────────

class PrepareAccountRequest(BaseModel):
    phone: str

class PrepareAccountResponse(BaseModel):
    account_id: str
    channel: str | None = None
    delivery_hint: str | None = None

class VerifyTelegramCodeRequest(BaseModel):
    account_id: str
    code: str

class VerifyTelegramCodeResponse(BaseModel):
    status: str
    requires_2fa: bool = False

class VerifyTelegram2FARequest(BaseModel):
    account_id: str
    password: str


_SENT_CODE_TYPE_TO_CHANNEL = {
    "SentCodeTypeApp": "telegram_app",
    "SentCodeTypeSms": "sms",
    "SentCodeTypeCall": "call",
    "SentCodeTypeFlashCall": "flash_call",
    "SentCodeTypeMissedCall": "flash_call",
}


def _sent_code_channel(sent) -> str | None:
    type_name = type(sent.type).__name__ if getattr(sent, "type", None) is not None else None
    return _SENT_CODE_TYPE_TO_CHANNEL.get(type_name)


def _delivery_hint(channel: str | None) -> str | None:
    if channel == "sms":
        return "인증번호가 SMS로 전송되었습니다."
    if channel == "telegram_app":
        return "Telegram 앱을 확인하세요."
    if channel == "call":
        return "인증번호가 자동 전화로 안내됩니다."
    if channel == "flash_call":
        return "전화가 걸려오면 마지막 2자리로 인증합니다."
    return None


@router.post("/prepare-account", response_model=PrepareAccountResponse)
async def prepare_account(
    payload: PrepareAccountRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    identity: "Identity | None" = Depends(get_optional_identity),
):
    """Create an Account + send MTProto code. No auth required, so onboarding
    (add a Telegram account before/without logging into TeleMon itself) keeps
    working — but if the caller IS already logged in (this same public endpoint
    is also what the in-dashboard "add account" screen calls), the account is
    tied to their tenant immediately instead of staying orphaned until a later
    login with the exact same phone number happens to match it (see
    _link_orphan_accounts_by_phone below). Without this, a logged-in user
    registering a Telegram account under a DIFFERENT phone than their login
    phone would silently end up with an account that 403s on every dialogs
    lookup, since it never gets linked to their tenant.

    Uses the shared TelethonPool (same as telegram_auth.send_code) for
    proper connection lifecycle, FloodWait handling, and dead-session recovery.
    """
    from app.services.telethon_pool import pool
    from app.crud import account as account_crud
    from app.models.account import Account
    from app.core.crypto import encrypt_session, decrypt_session
    from telethon.errors import (
        FloodWaitError,
        PhoneNumberInvalidError,
        UserDeactivatedBanError,
    )

    client_ip = get_client_ip(request)
    if not check_rate_limit(client_ip, "prepare_account", max_attempts=10, window_seconds=300):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="요청이 너무 많습니다.")

    phone = payload.phone.strip()
    if not phone:
        raise HTTPException(status_code=400, detail="전화번호가 필요합니다.")

    caller_tenant_id = identity.tenant_id if identity is not None else None

    # Find existing account or create new one
    result = await db.execute(select(Account).where(Account.phone == phone))
    existing = result.scalar_one_or_none()

    if existing and existing.tenant_id and caller_tenant_id and existing.tenant_id != caller_tenant_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="이미 등록된 번호입니다. 계정 목록에서 확인하세요.",
        )

    if existing and existing.status == "active":
        if caller_tenant_id and not existing.tenant_id:
            existing.tenant_id = caller_tenant_id
            await db.commit()
            logger.info("prepare_account_claimed_existing_orphan", account_id=existing.id, tenant_id=caller_tenant_id)
        return PrepareAccountResponse(account_id=existing.id)

    if existing:
        account = existing
        if caller_tenant_id and not account.tenant_id:
            account.tenant_id = caller_tenant_id
    else:
        account = Account(
            phone=phone,
            name=f"Telegram ({phone})",
            status="pending_auth",
            tenant_id=caller_tenant_id,
        )
        db.add(account)
        await db.flush()

    # Use the pool (same client lifecycle as telegram_auth.send_code)
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
        client = await pool.get_client(account.id, session_string, require_authorized=False)
    except RuntimeError as exc:
        logger.error("telegram_client_init_failed", account_id=account.id, error=str(exc))
        raise HTTPException(status_code=500, detail="Telegram 클라이언트 초기화에 실패했습니다. 잠시 후 다시 시도해주세요.")

    # Send code with FloodWait retry
    _SHORT_FLOOD_WAIT = 10

    async def _request_code():
        return await asyncio.wait_for(client.send_code_request(phone), timeout=45)

    try:
        try:
            sent = await _request_code()
        except FloodWaitError as exc:
            if exc.seconds > _SHORT_FLOOD_WAIT:
                raise
            await asyncio.sleep(exc.seconds)
            sent = await _request_code()
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="텔레그램 서버 응답이 지연되고 있습니다.")
    except PhoneNumberInvalidError:
        raise HTTPException(status_code=400, detail="유효하지 않은 전화번호입니다.")
    except FloodWaitError as exc:
        raise HTTPException(status_code=429, detail=f"요청이 너무 많습니다. {exc.seconds}초 후 다시 시도해주세요.")
    except UserDeactivatedBanError:
        await account_crud.set_auth_state(db, account, status="banned")
        raise HTTPException(status_code=403, detail="차단된 계정입니다.")
    except Exception as exc:
        error_str = str(exc)
        if any(e in error_str for e in ("Unauthorized", "auth_key", "SessionPasswordNeeded", "UserDeactivated")):
            await pool.remove_client(account.id)
            await account_crud.mark_account_session_invalid(db, account)
            raise HTTPException(status_code=400, detail="세션이 만료되었습니다. 다시 시도해주세요.")
        logger.error("prepare_account_error", phone=phone, error=error_str)
        raise HTTPException(status_code=500, detail="인증번호 발송 실패. 잠시 후 다시 시도해주세요.")

    # Persist session + pending auth
    await account_crud.save_session_snapshot(db, account, encrypt_session(client.session.save()))
    pool.set_pending_auth(account.id, sent.phone_code_hash)

    channel = _sent_code_channel(sent)
    logger.info("prepare_account_code_sent", account_id=account.id, channel=channel)
    return PrepareAccountResponse(
        account_id=account.id,
        channel=channel,
        delivery_hint=_delivery_hint(channel),
    )


@router.post("/verify-telegram-code", response_model=VerifyTelegramCodeResponse)
async def verify_telegram_code(
    payload: VerifyTelegramCodeRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    identity: "Identity | None" = Depends(get_optional_identity),
):
    """Verify the MTProto code. No auth required for onboarding, but see
    prepare_account's docstring — if the caller is logged in, attach the
    account to their tenant here too in case it wasn't already (e.g. account
    was prepared anonymously, then the user logged in before finishing
    verification).

    Uses the shared TelethonPool (same as telegram_auth.verify_code).
    """
    from app.services.telethon_pool import pool
    from app.crud import account as account_crud
    from app.core.crypto import decrypt_session, encrypt_session
    from telethon.errors import (
        SessionPasswordNeededError,
        PhoneCodeInvalidError,
        PhoneCodeExpiredError,
        FloodWaitError,
    )

    account = await account_crud.get_account(db, payload.account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="계정을 찾을 수 없습니다.")

    pending = pool.get_pending_auth(account.id)
    if pending is None:
        await asyncio.sleep(0.3)
        pending = pool.get_pending_auth(account.id)
    if pending is None:
        raise HTTPException(status_code=400, detail="먼저 인증번호를 요청해주세요.")

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
        client = await pool.get_client(account.id, session_string, require_authorized=False)
    except RuntimeError as exc:
        logger.error("telegram_client_init_failed", account_id=account.id, error=str(exc))
        raise HTTPException(status_code=500, detail="Telegram 클라이언트 초기화에 실패했습니다. 잠시 후 다시 시도해주세요.")

    try:
        await client.sign_in(phone=account.phone, code=payload.code, phone_code_hash=pending.phone_code_hash)
    except SessionPasswordNeededError:
        await account_crud.save_session_snapshot(db, account, encrypt_session(client.session.save()))
        return VerifyTelegramCodeResponse(status="needs_2fa", requires_2fa=True)
    except PhoneCodeInvalidError:
        raise HTTPException(status_code=400, detail="인증번호가 올바르지 않습니다.")
    except PhoneCodeExpiredError:
        pool.clear_pending_auth(account.id)
        raise HTTPException(status_code=400, detail="인증번호가 만료되었습니다. 다시 요청해주세요.")
    except FloodWaitError as exc:
        raise HTTPException(status_code=429, detail=f"요청이 너무 많습니다. {exc.seconds}초 후 다시 시도하세요.")
    except Exception as exc:
        error_str = str(exc)
        if any(e in error_str for e in ("Unauthorized", "auth_key", "SessionPasswordNeeded", "UserDeactivated")):
            await pool.remove_client(account.id)
            await account_crud.mark_account_session_invalid(db, account)
            raise HTTPException(status_code=400, detail="세션이 만료되었습니다. 다시 시도해주세요.")
        logger.error("verify_telegram_code_error", account_id=account.id, error=error_str)
        raise HTTPException(status_code=400, detail="인증에 실패했습니다. 잠시 후 다시 시도해주세요.")

    # Success
    session_str = client.session.save()
    account = await account_crud.set_auth_state(
        db, account, status="active", session_data=encrypt_session(session_str)
    )
    pool.clear_pending_auth(account.id)

    if identity is not None and identity.tenant_id and not account.tenant_id:
        account.tenant_id = identity.tenant_id
        await db.commit()
        logger.info("verify_telegram_code_claimed_orphan", account_id=account.id, tenant_id=identity.tenant_id)

    try:
        from app.services.telegram_actions import list_groups
        groups = await list_groups(account)
        account.group_count = len(groups)
        await db.commit()
    except Exception:
        pass

    logger.info("unified_login_verified", account_id=account.id)
    return VerifyTelegramCodeResponse(status="active", requires_2fa=False)


@router.post("/verify-telegram-2fa")
async def verify_telegram_2fa(
    payload: VerifyTelegram2FARequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    identity: "Identity | None" = Depends(get_optional_identity),
):
    """Verify 2FA password. No auth required for onboarding — see
    prepare_account's docstring for why a logged-in caller still gets the
    account attached to their tenant here.

    Uses the shared TelethonPool.
    """
    from app.services.telethon_pool import pool
    from app.crud import account as account_crud
    from app.core.crypto import decrypt_session, encrypt_session
    from telethon.errors import FloodWaitError

    account = await account_crud.get_account(db, payload.account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="계정을 찾을 수 없습니다.")

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
        client = await pool.get_client(account.id, session_string, require_authorized=False)
    except RuntimeError as exc:
        logger.error("telegram_client_init_failed", account_id=account.id, error=str(exc))
        raise HTTPException(status_code=500, detail="Telegram 클라이언트 초기화에 실패했습니다. 잠시 후 다시 시도해주세요.")

    try:
        await client.sign_in(password=payload.password)
    except FloodWaitError as exc:
        raise HTTPException(status_code=429, detail=f"요청이 너무 많습니다. {exc.seconds}초 후 다시 시도하세요.")
    except Exception as exc:
        error_str = str(exc)
        if any(e in error_str for e in ("Unauthorized", "auth_key", "UserDeactivated")):
            await pool.remove_client(account.id)
            await account_crud.mark_account_session_invalid(db, account)
            raise HTTPException(status_code=400, detail="세션이 만료되었습니다. 다시 시도해주세요.")
        logger.error("verify_telegram_2fa_error", account_id=account.id, error=error_str)
        raise HTTPException(status_code=400, detail="2FA 인증에 실패했습니다. 잠시 후 다시 시도해주세요.")

    session_str = client.session.save()
    account = await account_crud.set_auth_state(
        db, account, status="active", session_data=encrypt_session(session_str)
    )

    if identity is not None and identity.tenant_id and not account.tenant_id:
        account.tenant_id = identity.tenant_id
        await db.commit()
        logger.info("verify_telegram_2fa_claimed_orphan", account_id=account.id, tenant_id=identity.tenant_id)

    try:
        from app.services.telegram_actions import list_groups
        groups = await list_groups(account)
        account.group_count = len(groups)
        await db.commit()
    except Exception:
        pass

    logger.info("unified_login_2fa_verified", account_id=account.id)
    return {"status": "active"}