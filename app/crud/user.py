import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.limits import OTP_EXPIRE_MINUTES, OTP_MAX_ATTEMPTS, OTP_RESEND_COOLDOWN_SECONDS
from app.core.security import hash_otp_code
from app.models.user import PhoneVerification, User


def utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def get_pending_verification(db: AsyncSession, phone: str) -> PhoneVerification | None:
    result = await db.execute(select(PhoneVerification).where(PhoneVerification.phone == phone))
    return result.scalar_one_or_none()


async def seconds_until_next_code_allowed(db: AsyncSession, phone: str) -> float:
    existing = await get_pending_verification(db, phone)
    if existing is None:
        return 0
    elapsed = (utcnow_naive() - existing.created_at).total_seconds()
    return max(0.0, OTP_RESEND_COOLDOWN_SECONDS - elapsed)


async def upsert_verification_code(db: AsyncSession, phone: str, code: str) -> PhoneVerification:
    existing = await get_pending_verification(db, phone)
    if existing is not None:
        await db.delete(existing)
        await db.flush()
    verification = PhoneVerification(
        phone=phone,
        code_hash=hash_otp_code(code),
        expires_at=utcnow_naive() + timedelta(minutes=OTP_EXPIRE_MINUTES),
        attempts=0,
    )
    db.add(verification)
    await db.commit()
    await db.refresh(verification)
    return verification


async def verify_code(db: AsyncSession, phone: str, code: str) -> bool:
    """Returns True for a correct, unexpired code with attempts still remaining, and
    deletes it so it can't be replayed. Returns False for anything else (no pending
    code, expired, attempt cap reached, or wrong code) — a wrong guess still increments
    the persisted attempt counter so repeated guessing eventually locks the code out
    even across separate requests, rather than only within one process's memory."""
    verification = await get_pending_verification(db, phone)
    if verification is None:
        return False
    if verification.expires_at < utcnow_naive() or verification.attempts >= OTP_MAX_ATTEMPTS:
        await db.delete(verification)
        await db.commit()
        return False
    if not secrets.compare_digest(hash_otp_code(code), verification.code_hash):
        verification.attempts += 1
        await db.commit()
        return False
    await db.delete(verification)
    await db.commit()
    return True


async def get_user_by_phone(db: AsyncSession, phone: str) -> User | None:
    result = await db.execute(select(User).where(User.phone == phone))
    return result.scalar_one_or_none()


async def get_or_create_user(db: AsyncSession, phone: str) -> User:
    user = await get_user_by_phone(db, phone)
    if user is None:
        user = User(phone=phone)
        db.add(user)
        await db.commit()
        await db.refresh(user)
    return user


async def set_api_key_hash(db: AsyncSession, user: User, api_key_hash: str) -> User:
    user.api_key_hash = api_key_hash
    await db.commit()
    await db.refresh(user)
    return user


async def get_by_api_key_hash(db: AsyncSession, api_key_hash: str) -> User | None:
    result = await db.execute(select(User).where(User.api_key_hash == api_key_hash))
    return result.scalar_one_or_none()


async def get_user(db: AsyncSession, user_id: str) -> User | None:
    return await db.get(User, user_id)


async def touch_last_login(db: AsyncSession, user: User) -> User:
    user.last_login = utcnow_naive()
    await db.commit()
    await db.refresh(user)
    return user


async def list_users(db: AsyncSession) -> list[User]:
    result = await db.execute(select(User).order_by(User.created_at.desc()))
    return list(result.scalars().all())


async def set_active(db: AsyncSession, user: User, is_active: bool) -> User:
    user.is_active = is_active
    await db.commit()
    await db.refresh(user)
    return user
