"""Shared "create a pending USDT purchase" logic.

This is the one place that upserts a Tenant into subscription_status="pending"
with a payment_ref, so usdt_watcher.py's memo-matching can find it later. Used
by both the public web endpoint (app/api/usdt_payment.py's request_api_key)
and the Telegram bot purchase flow (app/services/bot_account_service.py) — a
single implementation, not two parallel ones.
"""

import secrets

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.crud import user as user_crud
from app.models.tenant import Tenant
from app.models.user import User
from app.models.referral import ReferralCode

logger = get_logger(__name__)


class PurchaseConflict(Exception):
    """Raised when the target tenant already has an active subscription."""


def generate_payment_ref() -> str:
    return f"TM-{secrets.token_hex(4).upper()}"


# telegram_user_id -> referral_code, set by the bot's /start ref_<code> deep
# link before any Tenant exists yet for that user. Consumed the moment a
# Tenant is actually created below. Process-local (single polling instance),
# same pattern as telegram_bot_service._active_ai_chat_users.
_pending_referrals: dict[int, str] = {}


def set_pending_referral(telegram_user_id: int, referral_code: str) -> None:
    _pending_referrals[telegram_user_id] = referral_code


def _pop_pending_referral(telegram_user_id: int) -> str | None:
    return _pending_referrals.pop(telegram_user_id, None)


def _telegram_user_id_from_phone(phone: str) -> int | None:
    if not phone.startswith("tg_"):
        return None
    try:
        return int(phone[len("tg_"):])
    except ValueError:
        return None


async def upsert_pending_tenant(
    db: AsyncSession,
    plan: str,
    payment_ref: str,
    phone: str = "",
) -> Tenant:
    """Find-or-create a Tenant and put it into a pending-purchase state for `plan`.

    `phone=""` reproduces the pre-existing anonymous web-purchase behavior: the
    tenant is stored under a synthetic `pending-<payment_ref>` identifier and no
    User record is created. A non-empty `phone` (a real phone number, or the
    bot's `tg_<telegram_user_id>` identifier) is used verbatim for both lookup
    and storage, and a matching User is found-or-created so the payer has a
    recoverable identity.

    Raises PurchaseConflict if the tenant already has an active subscription.
    Caller is responsible for committing the session (matches the pre-existing
    one-commit-per-request contract).
    """
    result = await db.execute(select(Tenant).where(Tenant.phone == phone))
    tenant = result.scalar_one_or_none()

    if tenant is None:
        referred_by = None
        tg_id = _telegram_user_id_from_phone(phone) if phone else None
        if tg_id is not None:
            referral_code = _pop_pending_referral(tg_id)
            if referral_code:
                referrer = (
                    await db.execute(select(Tenant).where(Tenant.referral_code == referral_code))
                ).scalar_one_or_none()
                if referrer is not None:
                    referred_by = referrer.id

        tenant = Tenant(
            phone=phone or f"pending-{payment_ref}",
            plan=plan,
            subscription_status="pending",
            payment_ref=payment_ref,
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
    else:
        if tenant.subscription_status == "active":
            raise PurchaseConflict(
                "이미 활성화된 요금제가 있습니다. 추가 결제가 필요하시면 고객지원으로 문의해주세요."
            )
        tenant.plan = plan
        tenant.subscription_status = "pending"
        tenant.payment_ref = payment_ref

    if phone:
        user = await user_crud.get_user_by_phone(db, phone)
        if user is None:
            user = User(phone=phone)
            db.add(user)
            await db.flush()
            logger.info("user_created_for_paid_signup", identifier=phone, tenant_plan=plan)

    await db.commit()
    await db.refresh(tenant)
    return tenant
