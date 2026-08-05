"""Server-side AI credit management — character-based billing.

Free tenants get 10,000 characters per day (input + output combined).
Paid tenants get monthly character pools sized per plan.

Credit costs: 1 character = 1 unit. The chat endpoint counts actual
input + output characters and deducts that amount.
"""

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import utcnow_naive
from app.models.tenant import Tenant

FREE_REFILL_INTERVAL = timedelta(days=1)
FREE_DAILY_CHARACTERS = 10_000

# Monthly character pool + manual-reset allowance, per paid plan.
PLAN_MONTHLY_CHARACTERS: dict[str, int] = {
    "pro": 500_000,
    "max": 2_000_000,
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
        tenant.ai_credits_remaining = FREE_DAILY_CHARACTERS
        tenant.ai_last_refill_at = now
    elif tenant.plan in PLAN_MONTHLY_CHARACTERS and tenant.ai_credits_remaining <= 0:
        tenant.ai_credits_remaining = PLAN_MONTHLY_CHARACTERS[tenant.plan]
        tenant.ai_credits_reset_tokens = PLAN_RESET_TOKENS[tenant.plan]
        tenant.ai_last_refill_at = now
    await db.commit()


async def check_and_deduct_characters(
    tenant: Tenant,
    db: AsyncSession,
    char_count: int,
) -> tuple[bool, int]:
    """Check if tenant has enough characters, deduct if yes.

    Returns (ok, remaining).
    """
    if tenant.plan == "admin":
        return True, 999999

    if tenant.plan == "free":
        _refill_if_needed(tenant)

    available = tenant.ai_credits_remaining

    if available < char_count:
        return False, available

    tenant.ai_credits_remaining -= char_count
    await db.commit()
    return True, tenant.ai_credits_remaining


async def check_and_deduct_credits(
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
    if tenant.plan not in PLAN_MONTHLY_CHARACTERS or tenant.ai_credits_reset_tokens <= 0:
        return False, tenant.ai_credits_remaining
    tenant.ai_credits_reset_tokens -= 1
    tenant.ai_credits_remaining = PLAN_MONTHLY_CHARACTERS[tenant.plan]
    await db.commit()
    return True, tenant.ai_credits_remaining


async def bulk_sync_credits(db: AsyncSession) -> int:
    """Background task: refill free tenants whose daily window has passed."""
    now = utcnow_naive()
    cutoff = now - FREE_REFILL_INTERVAL
    result = await db.execute(
        select(Tenant).where(
            Tenant.plan == "free",
            Tenant.is_active == True,
            Tenant.ai_credits_remaining < FREE_DAILY_CHARACTERS,
        )
    )
    refilled = 0
    for t in result.scalars().all():
        if t.ai_last_refill_at is None or t.ai_last_refill_at < cutoff:
            t.ai_credits_remaining = FREE_DAILY_CHARACTERS
            t.ai_last_refill_at = now
            refilled += 1
    await db.commit()
    return refilled


def _refill_if_needed(tenant: Tenant) -> None:
    """Inline refill check — called on every credit check for Free tenants."""
    if tenant.plan != "free":
        return
    if tenant.ai_credits_remaining >= FREE_DAILY_CHARACTERS:
        return
    now = utcnow_naive()
    if tenant.ai_last_refill_at is None:
        tenant.ai_credits_remaining = FREE_DAILY_CHARACTERS
        tenant.ai_last_refill_at = now
        return
    last = tenant.ai_last_refill_at
    if now - last >= FREE_REFILL_INTERVAL:
        tenant.ai_credits_remaining = FREE_DAILY_CHARACTERS
        tenant.ai_last_refill_at = now
