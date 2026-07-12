from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from datetime import datetime, timedelta, timezone

from app.api.deps import Identity, get_current_identity
from app.config import settings
from app.core.logging import get_logger
from app.core.plans import get_plan
from app.core.rate_limiter import check_rate_limit, get_client_ip, get_retry_after_seconds
from app.core.limits import (
    SEND_CODE_MAX_PER_IP,
    SEND_CODE_PER_IP_WINDOW,
    VERIFY_CODE_MAX_PER_IP,
    VERIFY_CODE_PER_IP_WINDOW,
)
from app.core.security import create_user_access_token, generate_otp_code, generate_user_api_key, hash_api_key
from app.crud import telegram_verification as verification_crud
from app.crud import user as user_crud
from app.database import get_db
from app.models.tenant import Tenant
from app.schemas.auth import (
    LoginWithApiKeyRequest,
    LoginWithApiKeyResponse,
    MeResponse,
    SendCodeRequest,
    SendCodeResponse,
    VerifyCodeRequest,
    VerifyCodeResponse,
)
from app.services.sms_service import SmsSendError, send_verification_sms
from app.services.usage_tracker import apply_plan_limits

router = APIRouter(prefix="/api/auth", tags=["auth"])
logger = get_logger(__name__)


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

    if tenant is None and settings.telegram_official_channel_id:
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
        trial_hours = plan_def["trial_hours"] if plan_def else 24
        trial_expires = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=trial_hours)
        tenant = Tenant(
            phone=payload.phone,
            plan="free",
            subscription_status="active",
            trial_expires_at=trial_expires,
        )
        db.add(tenant)
        await db.flush()
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
    user = await user_crud.get_by_api_key_hash(db, hash_api_key(payload.api_key))
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="유효하지 않거나 비활성화된 API 키입니다.")
    await user_crud.touch_last_login(db, user)
    logger.info("user_login_success", user_id=user.id)
    return LoginWithApiKeyResponse(access_token=create_user_access_token(user.id))


@router.get("/me", response_model=MeResponse)
async def me(
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    if identity.kind == "user" and identity.user is not None:
        from sqlalchemy import select
        from app.models.tenant import Tenant
        result = await db.execute(select(Tenant).where(Tenant.phone == identity.user.phone))
        tenant = result.scalar_one_or_none()
        return MeResponse(
            role="user",
            phone=identity.user.phone,
            subscription_status=tenant.subscription_status if tenant else None,
            plan=tenant.plan if tenant else None,
            trial_expires_at=tenant.trial_expires_at if tenant else None,
        )
    return MeResponse(role=identity.kind)
