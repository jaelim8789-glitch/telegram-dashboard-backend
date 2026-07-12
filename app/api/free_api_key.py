"""Free API key issuance gated on official Telegram channel membership verification.

Two-step flow:
  1. POST /api/free-api-key/start  — creates a verification token, returns bot deep link
  2. POST /api/free-api-key/issue  — consumes a verified token, issues one free API key

The same TelegramChannelVerification model and membership check from the free-trial
signup gate are reused here.  A phone can receive at most one free API key.
No SMS OTP required — channel membership is the sole verification factor.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.logging import get_logger
from app.core.rate_limiter import check_rate_limit, get_retry_after_seconds
from app.crud import telegram_verification as verification_crud
from app.crud import user as user_crud
from app.database import get_db
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.telegram_verify import FreeApiKeyIssueRequest, TelegramVerifyStartResponse
from app.core.plans import get_plan
from app.core.security import generate_user_api_key, hash_api_key
from app.services.usage_tracker import apply_plan_limits

from datetime import datetime, timedelta, timezone

router = APIRouter(prefix="/api/free-api-key", tags=["free-api-key"])
logger = get_logger(__name__)


def utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@router.post("/start", response_model=TelegramVerifyStartResponse)
async def start(request: Request, db: AsyncSession = Depends(get_db)):
    client_ip = request.client.host if request.client else "unknown"
    if not check_rate_limit(client_ip, "free_api_key_start", max_attempts=10, window_seconds=300):
        retry_after = get_retry_after_seconds(client_ip, "free_api_key_start")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="잠시 후 다시 시도해주세요.",
            headers={"Retry-After": str(retry_after)},
        )

    if not settings.telegram_bot_username or not settings.telegram_official_channel_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="채널 인증 기능이 아직 설정되지 않았습니다. 잠시 후 다시 시도해주세요.",
        )

    row = await verification_crud.create_verification(db)
    channel_ref = settings.telegram_official_channel_id.lstrip("@")
    return TelegramVerifyStartResponse(
        token=row.id,
        bot_deep_link=f"https://t.me/{settings.telegram_bot_username}?start={row.id}",
        channel_url=f"https://t.me/{channel_ref}",
    )


@router.post("/issue")
async def issue(
    payload: FreeApiKeyIssueRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    client_ip = request.client.host if request.client else "unknown"
    if not check_rate_limit(client_ip, "free_api_key_issue", max_attempts=20, window_seconds=300):
        retry_after = get_retry_after_seconds(client_ip, "free_api_key_issue")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="잠시 후 다시 시도해주세요.",
            headers={"Retry-After": str(retry_after)},
        )

    if not settings.telegram_bot_token or not settings.telegram_official_channel_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="채널 인증 기능이 아직 설정되지 않았습니다.",
        )

    row = await verification_crud.get_verification(db, payload.token)
    if row is None:
        raise HTTPException(status_code=404, detail="인증 세션을 찾을 수 없거나 만료되었습니다.")

    if row.status != "verified":
        if row.telegram_user_id is None:
            detail = "먼저 텔레그램 봇을 통해 인증을 시작해주세요."
        else:
            detail = "채널 가입이 확인되지 않았습니다. 채널에 가입한 후 다시 시도해주세요."
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)

    if row.consumed_at is not None:
        raise HTTPException(status_code=409, detail="이 인증 토큰은 이미 사용되었습니다.")

    consumed = await verification_crud.consume_verified_token(db, payload.token)
    if not consumed:
        raise HTTPException(status_code=409, detail="이 인증 토큰은 이미 사용되었습니다.")

    if not payload.phone:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="전화번호는 필수 항목입니다.",
        )

    raw_key = generate_user_api_key()

    user = await user_crud.get_user_by_phone(db, payload.phone)
    if user is None:
        user = User(phone=payload.phone)
        db.add(user)
        await db.flush()

    if user.api_key_hash:
        logger.info("free_api_key_already_issued", phone=payload.phone)
        return {"api_key": None, "detail": "이미 무료 API 키가 발급되었습니다.", "already_issued": True}

    user.api_key_hash = hash_api_key(raw_key)
    await db.flush()

    await _get_or_create_free_tenant(db, payload.phone)

    await db.commit()
    await db.refresh(user)

    logger.info("free_api_key_issued", token_used=payload.token)
    return {"api_key": raw_key, "detail": "무료 API 키가 발급되었습니다.", "already_issued": False}


async def _get_or_create_free_tenant(db: AsyncSession, phone: str | None) -> Tenant | None:
    if not phone:
        return None

    from sqlalchemy import select

    result = await db.execute(select(Tenant).where(Tenant.phone == phone))
    tenant = result.scalar_one_or_none()

    if tenant is None:
        plan_def = get_plan("free")
        trial_hours = (plan_def["trial_days"] * 24) if plan_def else 24
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

    return tenant
