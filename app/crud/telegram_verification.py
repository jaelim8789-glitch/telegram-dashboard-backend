from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
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


async def mark_verified(db: AsyncSession, token: str) -> bool:
    """Atomic-conditional mark: only a ``linked``, not-yet-verified, unexpired row
    gets promoted to ``verified``. Returns True iff exactly one row was updated."""
    cutoff = utcnow_naive() - timedelta(minutes=TOKEN_TTL_MINUTES)
    now = utcnow_naive()
    result = await db.execute(
        update(TelegramChannelVerification)
        .where(
            TelegramChannelVerification.id == token,
            TelegramChannelVerification.status == "linked",
            TelegramChannelVerification.verified_at.is_(None),
            TelegramChannelVerification.created_at > cutoff,
        )
        .values(status="verified", verified_at=now)
        .returning(TelegramChannelVerification.id)
    )
    await db.commit()
    return result.scalar_one_or_none() is not None


async def consume_verified_token(db: AsyncSession, token: str) -> bool:
    """Atomically spend a verified, not-yet-consumed, unexpired token. Uses a single
    conditional UPDATE so two concurrent callers cannot both succeed — the database
    serialises the two UPDATEs and only the first sees ``consumed_at IS NULL``.
    Returns True iff exactly one row was updated."""
    cutoff = utcnow_naive() - timedelta(minutes=TOKEN_TTL_MINUTES)
    now = utcnow_naive()
    result = await db.execute(
        update(TelegramChannelVerification)
        .where(
            TelegramChannelVerification.id == token,
            TelegramChannelVerification.status == "verified",
            TelegramChannelVerification.consumed_at.is_(None),
            TelegramChannelVerification.created_at > cutoff,
        )
        .values(consumed_at=now)
        .returning(TelegramChannelVerification.id)
    )
    await db.commit()
    return result.scalar_one_or_none() is not None
