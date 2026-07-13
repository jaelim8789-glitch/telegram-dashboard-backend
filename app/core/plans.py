"""Canonical PLAN_CATALOG — single source of truth for pricing and limits.

Every module that needs plan prices, feature limits, or billing intervals
imports from here.  No duplicated PLAN_PRICES_USDT / PLAN_LIMITS dicts.
"""

from typing import Literal

PlanId = Literal["free", "pro", "team"]
BillingInterval = Literal["monthly", "quarterly"]

PlanDef = dict

PLAN_CATALOG: dict[PlanId, PlanDef] = {
    "free": {
        "name": "Free",
        "description": "Starter plan with a free trial period",
        "trial_days": 1,
        "prices_usdt": {
            "monthly": 0,
        },
        "limits": {
            "max_accounts": 1,
            "max_auto_reply_rules": 3,
            "max_reply_macros": 1,
            "monthly_message_limit": 100,
            "monthly_auto_reply_limit": 100,
            "cooldown_minimum_minutes": 60,
            "can_broadcast": True,
            "can_schedule": False,
            "can_attach_images": False,
            "can_export_data": False,
        },
        "features": [
            "1 account",
            "3 auto-reply rules",
            "100 replies/month",
            "60 min cooldown",
        ],
    },
    "pro": {
        "name": "Pro",
        "description": "$100/month — 10 accounts",
        "trial_days": 0,
        "prices_usdt": {
            "monthly": 100,
        },
        "limits": {
            "max_accounts": 10,
            "max_auto_reply_rules": 100,
            "max_reply_macros": 50,
            "monthly_message_limit": 50000,
            "monthly_auto_reply_limit": 50000,
            "cooldown_minimum_minutes": 0,
            "can_broadcast": True,
            "can_schedule": True,
            "can_attach_images": True,
            "can_export_data": True,
        },
        "features": [
            "10 accounts",
            "100 auto-reply rules",
            "50 reply macros",
            "50,000 replies/month",
            "Message broadcast & scheduling",
            "Image attachments",
            "Delivery analytics",
            "Priority support",
        ],
    },
    "team": {
        "name": "Team",
        "description": "$199/quarter — 20 accounts",
        "trial_days": 0,
        "prices_usdt": {
            "quarterly": 199,
        },
        "limits": {
            "max_accounts": 20,
            "max_auto_reply_rules": 250,
            "max_reply_macros": 100,
            "monthly_message_limit": 200000,
            "monthly_auto_reply_limit": 200000,
            "cooldown_minimum_minutes": 0,
            "can_broadcast": True,
            "can_schedule": True,
            "can_attach_images": True,
            "can_export_data": True,
        },
        "features": [
            "20 accounts",
            "250 auto-reply rules",
            "100 reply macros",
            "200,000 replies/month",
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
        raise ValueError("해당 요금제는 더 이상 제공되지 않습니다. Pro ($100/월) 또는 Team ($199/분기)을 선택해주세요.")
    if plan_id not in PLAN_CATALOG:
        raise ValueError("유효하지 않은 요금제입니다.")
    return plan_id
