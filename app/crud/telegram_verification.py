from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.telegram_verification import TelegramChannelVerification

# A pending/linked/verified row older than this is treated as expired — the token
# is single-purpose for one signup attempt, not a long-lived credential.
TOKEN_TTL_MINUTES = 30


def utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def create_verification(db: AsyncSession) -> TelegramChannelVerification:
    row = TelegramChannelVerification(status="pending")
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def get_verification(db: AsyncSession, token: str) -> TelegramChannelVerification | None:
    row = await db.get(TelegramChannelVerification, token)
    if row is None:
        return None
    if row.created_at < utcnow_naive() - timedelta(minutes=TOKEN_TTL_MINUTES):
        return None
    return row


async def link_telegram_user(db: AsyncSession, token: str, telegram_user_id: int) -> bool:
    """Called only from the bot's /start handler, where telegram_user_id comes from a
    real Telegram Update — never from an HTTP request the frontend could forge.
    Returns False if the token doesn't exist or has expired."""
    row = await get_verification(db, token)
    if row is None:
        return False
    row.telegram_user_id = telegram_user_id
    row.status = "linked"
    row.linked_at = utcnow_naive()
    await db.commit()
    return True


async def mark_verified(db: AsyncSession, row: TelegramChannelVerification) -> None:
    row.status = "verified"
    row.verified_at = utcnow_naive()
    await db.commit()


async def consume_verified_token(db: AsyncSession, token: str) -> bool:
    """Atomically spend a verified, not-yet-consumed, unexpired token. Returns False
    (and leaves the row untouched) for anything else — missing, expired, not yet
    verified, or already consumed — so a token can fund at most one trial ever."""
    row = await get_verification(db, token)
    if row is None:
        return False
    if row.status != "verified" or row.consumed_at is not None:
        return False
    row.consumed_at = utcnow_naive()
    await db.commit()
    return True
