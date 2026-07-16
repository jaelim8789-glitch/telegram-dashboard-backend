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

logger = get_logger(__name__)


class PurchaseConflict(Exception):
    """Raised when the target tenant already has an active subscription."""


def generate_payment_ref() -> str:
    return f"TM-{secrets.token_hex(4).upper()}"


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
        tenant = Tenant(
            phone=phone or f"pending-{payment_ref}",
            plan=plan,
            subscription_status="pending",
            payment_ref=payment_ref,
        )
        db.add(tenant)
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
