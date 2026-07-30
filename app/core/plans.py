"""Canonical PLAN_CATALOG  single source of truth for pricing and limits.

Every module that needs plan prices, feature limits, or billing intervals
imports from here.  No duplicated PLAN_PRICES_USDT / PLAN_LIMITS dicts.
"""

from typing import Literal

PlanId = Literal["free", "pro"]
BillingInterval = Literal["monthly", "quarterly"]

PlanDef = dict

PLAN_CATALOG: dict[PlanId, PlanDef] = {
    "free": {
        "name": "Free",
        "description": "1 account  AI chat 10/5h  basic messaging",
        "trial_days": 0,
        "prices_usdt": {
            "monthly": 0,
        },
        "limits": {
            "max_accounts": 1,
            "max_auto_reply_rules": 3,
            "max_reply_macros": 1,
            "monthly_message_limit": 100,
            "monthly_auto_reply_limit": 100,
            "monthly_ai_chat_limit": 0,  # Replaced by 5h/10 refill on frontend
            "monthly_ai_credits": 0,
            "cooldown_minimum_minutes": 60,
            "can_broadcast": False,
            "can_schedule": False,
            "can_attach_images": False,
            "can_export_data": False,
        },
        "features": [
            "1 account",
            "3 auto-reply rules",
            "1 reply macro",
            "100 messages/month",
            "AI chat 10 per 5h refill",
            "Basic analytics",
        ],
    },
    "pro": {
        "name": "Pro",
        "description": "$99/month  10 accounts  unlimited macros  AI credits 100K/mo",
        "trial_days": 0,
        "prices_usdt": {
            "monthly": 99,
        },
        "limits": {
            "max_accounts": 10,
            "max_auto_reply_rules": 100,
            "max_reply_macros": 999999,  # Unlimited
            "monthly_message_limit": 50000,
            "monthly_auto_reply_limit": 50000,
            "monthly_ai_chat_limit": 100000,  # Credits
            "monthly_ai_credits": 100000,
            "ai_reset_tokens": 3,
            "cooldown_minimum_minutes": 0,
            "can_broadcast": True,
            "can_schedule": True,
            "can_attach_images": True,
            "can_export_data": True,
        },
        "features": [
            "10 accounts",
            "100 auto-reply rules",
            "Unlimited reply macros",
            "50,000 messages/month",
            "100,000 AI credits/month",
            "3 AI credit resets/month",
            "Message broadcast & scheduling",
            "Image attachments",
            "Delivery analytics",
            "Priority support",
        ],
    },
}


def get_plan(plan_id: str) -> PlanDef | None:
    return PLAN_CATALOG.get(plan_id)


def get_plan_price_usdt(plan_id: str, billing: BillingInterval = "monthly") -> int | None:
    plan = get_plan(plan_id)
    if plan is None:
        return None
    return plan["prices_usdt"].get(billing)


def get_plan_limits(plan_id: str) -> dict | None:
    plan = get_plan(plan_id)
    if plan is None:
        return None
    return dict(plan["limits"])


def is_deprecated_plan(plan_id: str) -> bool:
    return plan_id in ("basic", "enterprise")


def validate_plan_id(plan_id: str) -> str:
    """Validate and return the plan ID, raising ValueError if it's invalid or deprecated.

    Returns the validated plan_id. Raises ValueError with a user-facing message.
    """
    if is_deprecated_plan(plan_id):
        raise ValueError("     . Pro ($100/)  Team ($199/) .")
    if plan_id not in PLAN_CATALOG:
        raise ValueError("  .")
    return plan_id
