"""Canonical PLAN_CATALOG -- single source of truth for pricing and limits.

Every module that needs plan prices, feature limits, or billing intervals
imports from here. No duplicated PLAN_PRICES_USDT / PLAN_LIMITS dicts.
"""

from typing import Literal

PlanId = Literal["free", "pro", "max"]
BillingInterval = Literal["monthly", "quarterly"]

PlanDef = dict

PLAN_CATALOG: dict[PlanId, PlanDef] = {
    "free": {
        "name": "Free",
        "description": "All features, AI chat 10/5h refill",
        "trial_days": 0,
        "prices_usdt": {
            "monthly": 0,
        },
        "limits": {
            "max_accounts": 3,
            "max_auto_reply_rules": 50,
            "max_reply_macros": 999999,
            "monthly_message_limit": 999999,
            "monthly_auto_reply_limit": 999999,
            "monthly_ai_chat_limit": 0,
            "monthly_ai_credits": 0,
            "cooldown_minimum_minutes": 0,
            "can_broadcast": True,
            "can_schedule": True,
            "can_attach_images": True,
            "can_export_data": True,
        },
        "features": [
            "3 accounts",
            "Unlimited auto-reply rules",
            "Unlimited reply macros",
            "Unlimited messages",
            "AI chat 10 per 5h refill",
            "Message broadcast & scheduling",
            "Image attachments",
            "Full analytics",
        ],
    },
    "pro": {
        "name": "Pro",
        "description": "$30/month, 3 accounts, AI credits 50K/mo",
        "trial_days": 0,
        "prices_usdt": {
            "monthly": 30,
        },
        "limits": {
            "max_accounts": 3,
            "max_auto_reply_rules": 30,
            "max_reply_macros": 999999,  # Unlimited
            "monthly_message_limit": 15000,
            "monthly_auto_reply_limit": 15000,
            "monthly_ai_chat_limit": 50000,  # Credits
            "monthly_ai_credits": 50000,
            "ai_reset_tokens": 1,
            "cooldown_minimum_minutes": 0,
            "can_broadcast": True,
            "can_schedule": True,
            "can_attach_images": True,
            "can_export_data": True,
        },
        "features": [
            "3 accounts",
            "30 auto-reply rules",
            "Unlimited reply macros",
            "15,000 messages/month",
            "50,000 AI credits/month",
            "1 AI credit reset/month",
            "Message broadcast & scheduling",
            "Image attachments",
            "Delivery analytics",
        ],
    },
    "max": {
        "name": "Max",
        "description": "$99/month, 10 accounts, unlimited macros, AI credits 200K/mo",
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
            "monthly_ai_chat_limit": 200000,  # Credits
            "monthly_ai_credits": 200000,
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
            "200,000 AI credits/month",
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
    return plan_id in ("basic", "enterprise", "team", "starter")


def validate_plan_id(plan_id: str) -> str:
    """Validate and return the plan ID, raising ValueError if it's invalid or deprecated.

    Returns the validated plan_id. Raises ValueError with a user-facing message.
    """
    if is_deprecated_plan(plan_id):
        raise ValueError("더 이상 지원하지 않는 요금제입니다. Pro($30/월) 또는 Max($99/월)를 이용해주세요.")
    if plan_id not in PLAN_CATALOG:
        raise ValueError("유효하지 않은 요금제입니다.")
    return plan_id
