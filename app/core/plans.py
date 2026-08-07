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
        "description": "$70/month, 10 accounts, unlimited macros, AI credits 200K/mo",
        "trial_days": 0,
        "prices_usdt": {
            "monthly": 70,
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

# Feature flags per plan — the single source of truth for feature gating.
PLAN_FEATURES: dict[str, dict[str, bool]] = {
    "free": {
        "can_broadcast": False,
        "can_schedule": False,
        "can_attach_images": False,
        "can_export_data": False,
        "can_use_api": True,
        "ai_operator": False,
        "pixel_office": False,
        "analytics": False,
        "campaign": False,
        "scheduler": False,
        "voice": False,
        "priority_support": False,
    },
    "pro": {
        "can_broadcast": True,
        "can_schedule": True,
        "can_attach_images": True,
        "can_export_data": True,
        "can_use_api": True,
        "ai_operator": True,
        "pixel_office": False,
        "analytics": True,
        "campaign": True,
        "scheduler": True,
        "voice": False,
        "priority_support": False,
    },
    "max": {
        "can_broadcast": True,
        "can_schedule": True,
        "can_attach_images": True,
        "can_export_data": True,
        "can_use_api": True,
        "ai_operator": True,
        "pixel_office": True,
        "analytics": True,
        "campaign": True,
        "scheduler": True,
        "voice": True,
        "priority_support": True,
    },
}


def get_plan(plan_id: str) -> PlanDef | None:
    return PLAN_CATALOG.get(plan_id)


def get_plan_price_usdt(plan_id: str, billing: BillingInterval = "monthly") -> int | None:
    plan = get_plan(plan_id)
    if plan is None:
        return None
    return plan["prices_usdt"].get(billing)


def get_plan_limits(plan_id: str) -> dict:
    """Return a flat dict of all numeric limits + feature flags for a plan.

    Falls back to free-tier limits if plan_id is unknown.
    """
    plan = get_plan(plan_id)
    if plan is None:
        plan = get_plan("free")
    limits = plan.get("limits", {})
    return {
        "max_accounts": limits.get("max_accounts", 3),
        "max_auto_reply_rules": limits.get("max_auto_reply_rules", 50),
        "max_reply_macros": limits.get("max_reply_macros", 10),
        "monthly_message_limit": limits.get("monthly_message_limit", 999999),
        "monthly_auto_reply_limit": limits.get("monthly_auto_reply_limit", 999999),
        "monthly_ai_chat_limit": limits.get("monthly_ai_chat_limit", 0),
        "cooldown_minimum_minutes": limits.get("cooldown_minimum_minutes", 60),
        "ai_credits_remaining": limits.get("monthly_ai_credits", 0),
        "ai_credits_reset_tokens": limits.get("ai_reset_tokens", 0),
        "features": get_plan_features(plan_id),
    }


def has_feature(plan_id: str, feature: str) -> bool:
    """Check if a plan includes a specific feature."""
    features = PLAN_FEATURES.get(plan_id, PLAN_FEATURES.get("free", {}))
    return features.get(feature, False)


def get_plan_features(plan_id: str) -> dict[str, bool]:
    """Get all feature flags for a plan."""
    return PLAN_FEATURES.get(plan_id, PLAN_FEATURES.get("free", {}))


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
