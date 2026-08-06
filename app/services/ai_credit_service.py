"""Server-side AI credit management — character-based billing.

Free tenants get 10,000 credits, refilled every 6 hours.
Paid tenants get monthly credit pools sized per plan.

Credit costs: 1 credit = 1 character (input + output combined).
"""

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import utcnow_naive
from app.models.tenant import Tenant

FREE_REFILL_INTERVAL = timedelta(hours=6)
FREE_CREDITS_PER_REFILL = 10_000

# Promo cohort: anyone who signed up 2026-08-06..08 (inclusive) gets a
# boosted free-plan refill -- 30,000 credits every 3 hours instead of the
# standard 10,000/6h. Keyed off Tenant.created_at, so it's permanent for
# that cohort (not just a one-time bonus) until this block is removed.
_PROMO_COHORT_START = datetime(2026, 8, 6, 0, 0, 0)
_PROMO_COHORT_END = datetime(2026, 8, 9, 0, 0, 0)  # exclusive upper bound
_PROMO_REFILL_INTERVAL = timedelta(hours=3)
_PROMO_CREDITS_PER_REFILL = 30_000


def _is_promo_cohort(tenant: Tenant) -> bool:
    created = tenant.created_at
    if created is None:
        return False
    return _PROMO_COHORT_START <= created < _PROMO_COHORT_END


def _refill_amount_for(tenant: Tenant) -> int:
    return _PROMO_CREDITS_PER_REFILL if _is_promo_cohort(tenant) else FREE_CREDITS_PER_REFILL


def _refill_interval_for(tenant: Tenant) -> timedelta:
    return _PROMO_REFILL_INTERVAL if _is_promo_cohort(tenant) else FREE_REFILL_INTERVAL

# Monthly credit pool + manual-reset allowance, per paid plan.
# 1 credit = 1 character (input + output combined).
PLAN_MONTHLY_CREDITS: dict[str, int] = {
    "pro": 50_000_000,
    "max": 300_000_000,
}
PLAN_RESET_TOKENS: dict[str, int] = {
    "pro": 1,
    "max": 3,
}

# Legacy credit costs (kept for backward compatibility with non-chat features)
CREDIT_COST: dict[str, int] = {
    "chat": 50,
    "reply_assistant": 10,
    "broadcast_assistant": 15,
    "operations_report": 20,
}

SENSITIVE_MULTIPLIER = 2


async def ensure_initial_credits(tenant: Tenant, db: AsyncSession) -> None:
    """Initialize credits when a tenant is first created or plan changes."""
    now = utcnow_naive()
    if tenant.plan == "free" and tenant.ai_credits_remaining <= 0:
        tenant.ai_credits_remaining = _refill_amount_for(tenant)
        tenant.ai_last_refill_at = now
    elif tenant.plan in PLAN_MONTHLY_CREDITS and tenant.ai_credits_remaining <= 0:
        tenant.ai_credits_remaining = PLAN_MONTHLY_CREDITS[tenant.plan]
        tenant.ai_credits_reset_tokens = PLAN_RESET_TOKENS[tenant.plan]
        tenant.ai_last_refill_at = now
    await db.commit()


async def check_and_deduct_credits(
    tenant: Tenant,
    db: AsyncSession,
    credit_count: int,
) -> tuple[bool, int]:
    """Check if tenant has enough credits, deduct if yes.

    1 credit = 1 character. Returns (ok, remaining).
    """
    if tenant.plan == "admin":
        return True, 999999

    if tenant.plan == "free":
        _refill_if_needed(tenant)

    available = tenant.ai_credits_remaining

    if available < credit_count:
        return False, available

    tenant.ai_credits_remaining -= credit_count
    await db.commit()
    return True, tenant.ai_credits_remaining


async def check_and_deduct_legacy_credits(
    tenant: Tenant,
    db: AsyncSession,
    feature: str,
    is_sensitive: bool = False,
) -> tuple[bool, int]:
    """Check if tenant has enough credits, deduct if yes.  Returns (ok, remaining).

    Legacy wrapper — for non-chat features that still use fixed credit costs.
    """
    if tenant.plan == "admin":
        return True, 999999

    if tenant.plan == "free":
        _refill_if_needed(tenant)

    available = tenant.ai_credits_remaining

    base_cost = CREDIT_COST.get(feature, 50)
    cost = base_cost * (SENSITIVE_MULTIPLIER if is_sensitive else 1)

    if available < cost:
        return False, available

    tenant.ai_credits_remaining -= cost
    await db.commit()
    return True, tenant.ai_credits_remaining


async def get_remaining_credits(tenant: Tenant) -> int:
    """Return current remaining credits (with free refill check)."""
    if tenant.plan == "admin":
        return 999999
    if tenant.plan == "free":
        _refill_if_needed(tenant)
    return tenant.ai_credits_remaining


async def reset_credits(tenant: Tenant, db: AsyncSession) -> tuple[bool, int]:
    """Use a reset token to refill the tenant's plan credit pool. Returns (ok, remaining)."""
    if tenant.plan not in PLAN_MONTHLY_CREDITS or tenant.ai_credits_reset_tokens <= 0:
        return False, tenant.ai_credits_remaining
    tenant.ai_credits_reset_tokens -= 1
    tenant.ai_credits_remaining = PLAN_MONTHLY_CREDITS[tenant.plan]
    await db.commit()
    return True, tenant.ai_credits_remaining


async def bulk_sync_credits(db: AsyncSession) -> int:
    """Background task: refill free tenants whose window has passed.

    Loads all under-cap free tenants rather than pre-filtering by amount in
    SQL, since the promo cohort's cap (30,000) differs from the standard
    cap (10,000) per-row -- the actual "does this one need a refill yet"
    decision has to happen in Python where _refill_amount_for/_interval_for
    can see each tenant's created_at.
    """
    now = utcnow_naive()
    result = await db.execute(
        select(Tenant).where(
            Tenant.plan == "free",
            Tenant.is_active == True,
        )
    )
    refilled = 0
    for t in result.scalars().all():
        amount = _refill_amount_for(t)
        if t.ai_credits_remaining >= amount:
            continue
        interval = _refill_interval_for(t)
        if t.ai_last_refill_at is None or now - t.ai_last_refill_at >= interval:
            t.ai_credits_remaining = amount
            t.ai_last_refill_at = now
            refilled += 1
    await db.commit()
    return refilled


def _refill_if_needed(tenant: Tenant) -> None:
    """Inline refill check — called on every credit check for Free tenants."""
    if tenant.plan != "free":
        return
    amount = _refill_amount_for(tenant)
    if tenant.ai_credits_remaining >= amount:
        return
    now = utcnow_naive()
    if tenant.ai_last_refill_at is None:
        tenant.ai_credits_remaining = amount
        tenant.ai_last_refill_at = now
        return
    last = tenant.ai_last_refill_at
    if now - last >= _refill_interval_for(tenant):
        tenant.ai_credits_remaining = amount
        tenant.ai_last_refill_at = now
