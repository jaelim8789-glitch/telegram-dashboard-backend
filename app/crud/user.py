import secrets
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.limits import OTP_EXPIRE_MINUTES, OTP_MAX_ATTEMPTS, OTP_RESEND_COOLDOWN_SECONDS
from app.core.security import hash_otp_code
from app.core.time import utcnow_naive
from app.models.account import Account
from app.models.tenant import Tenant
from app.models.user import PhoneVerification, User


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


class UserWithTenantInfo:
    """Lightweight row wrapper combining a User with its matching Tenant (joined on
    phone — tenants aren't FK-linked to users, see Tenant model docstring) and its
    account count. Used only for the admin users list, which needs plan/subscription/
    account-count columns the bare User model doesn't have."""

    __slots__ = ("user", "plan", "subscription_status", "trial_expires_at", "account_count", "stars_balance")

    def __init__(
        self,
        user: User,
        plan: str | None,
        subscription_status: str | None,
        trial_expires_at: datetime | None,
        account_count: int,
        stars_balance: int,
    ) -> None:
        self.user = user
        self.plan = plan
        self.subscription_status = subscription_status
        self.trial_expires_at = trial_expires_at
        self.account_count = account_count
        self.stars_balance = stars_balance


async def list_users_with_tenant_info(db: AsyncSession, skip: int = 0, limit: int = 100) -> list[UserWithTenantInfo]:
    """Admin users list, enriched with plan/subscription/account-count so the admin
    console can show a real picture of each user instead of just phone + active flag."""
    account_counts_subq = (
        select(Account.tenant_id, func.count(Account.id).label("account_count"))
        .group_by(Account.tenant_id)
        .subquery()
    )
    result = await db.execute(
        select(User, Tenant, account_counts_subq.c.account_count)
        .outerjoin(Tenant, Tenant.phone == User.phone)
        .outerjoin(account_counts_subq, account_counts_subq.c.tenant_id == Tenant.id)
        .order_by(User.created_at.desc())
        .offset(skip)
        .limit(limit)
    )
    rows = []
    for user, tenant, account_count in result.all():
        rows.append(
            UserWithTenantInfo(
                user=user,
                plan=tenant.plan if tenant else None,
                subscription_status=tenant.subscription_status if tenant else None,
                trial_expires_at=tenant.trial_expires_at if tenant else None,
                account_count=account_count or 0,
                stars_balance=tenant.stars_balance if tenant else 0,
            )
        )
    return rows


async def set_active(db: AsyncSession, user: User, is_active: bool) -> User:
    user.is_active = is_active
    await db.commit()
    await db.refresh(user)
    return user


async def get_user_by_telegram_id(db: AsyncSession, telegram_id: int) -> User | None:
    result = await db.execute(select(User).where(User.telegram_id == telegram_id))
    return result.scalar_one_or_none()


async def get_or_create_user_by_telegram(
    db: AsyncSession,
    telegram_id: int,
    telegram_username: str | None,
    telegram_photo_url: str | None,
) -> User:
    user = await get_user_by_telegram_id(db, telegram_id)
    if user is None:
        user = User(
            telegram_id=telegram_id,
            telegram_username=telegram_username or "",
            telegram_photo_url=telegram_photo_url or "",
            phone=f"tg_{telegram_id}",
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
    else:
        if telegram_username and telegram_username != user.telegram_username:
            user.telegram_username = telegram_username
        if telegram_photo_url:
            user.telegram_photo_url = telegram_photo_url
        await db.commit()
        await db.refresh(user)
    return user
