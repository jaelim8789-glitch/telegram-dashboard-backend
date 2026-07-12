"""Official-channel membership verification gate for free-trial signup.

Never trust the frontend's word that a user joined the channel — every /check call
re-verifies against the Telegram Bot API server-side. See app/services/telegram_membership.py
for the fail-closed membership check itself.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.logging import get_logger
from app.core.rate_limiter import check_rate_limit, get_retry_after_seconds
from app.crud import telegram_verification as verification_crud
from app.database import get_db
from app.schemas.telegram_verify import (
    TelegramVerifyCheckRequest,
    TelegramVerifyCheckResponse,
    TelegramVerifyStartResponse,
)
from app.services.telegram_membership import MembershipCheckUnavailable, is_channel_member

router = APIRouter(prefix="/api/telegram-verify", tags=["telegram-verify"])
logger = get_logger(__name__)


@router.post("/start", response_model=TelegramVerifyStartResponse)
async def start(request: Request, db: AsyncSession = Depends(get_db)):
    client_ip = request.client.host if request.client else "unknown"
    if not check_rate_limit(client_ip, "telegram_verify_start", max_attempts=10, window_seconds=300):
        retry_after = get_retry_after_seconds(client_ip, "telegram_verify_start")
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


@router.post("/check", response_model=TelegramVerifyCheckResponse)
async def check(payload: TelegramVerifyCheckRequest, request: Request, db: AsyncSession = Depends(get_db)):
    client_ip = request.client.host if request.client else "unknown"
    if not check_rate_limit(client_ip, "telegram_verify_check", max_attempts=30, window_seconds=60):
        retry_after = get_retry_after_seconds(client_ip, "telegram_verify_check")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="너무 자주 확인하고 있습니다. 잠시 후 다시 시도해주세요.",
            headers={"Retry-After": str(retry_after)},
        )

    row = await verification_crud.get_verification(db, payload.token)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="인증 세션을 찾을 수 없거나 만료되었습니다.")

    if row.status == "verified":
        return TelegramVerifyCheckResponse(status="verified")

    if row.telegram_user_id is None:
        # Bot hasn't received /start <token> yet — not an error, frontend keeps polling.
        return TelegramVerifyCheckResponse(status="pending_bot_start")

    try:
        is_member = await is_channel_member(row.telegram_user_id)
    except MembershipCheckUnavailable:
        # Fail closed: never grant verification when we can't actually check.
        logger.warning("telegram_verify_check_unavailable", token=payload.token)
        return TelegramVerifyCheckResponse(status="unverified", reason="membership_check_unavailable")

    if not is_member:
        return TelegramVerifyCheckResponse(status="unverified", reason="not_a_member")

    await verification_crud.mark_verified(db, row)
    return TelegramVerifyCheckResponse(status="verified")
