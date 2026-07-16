"""Usage tracking for per-tenant billing and rate limiting.

This module provides middleware and utilities to track usage across
all paid actions (broadcast, auto_reply, reply_macro, api_call).
It enables both subscription-based and usage-based (credit) billing models.

Plan limits are sourced from app.core.plans (PLAN_CATALOG).
"""

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.plans import get_plan_limits, validate_plan_id
from app.database import async_session_maker
from app.models.tenant import Tenant, UsageRecord

logger = get_logger(__name__)


def utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ─── Usage Tracking ───────────────────────────────────────────────────


async def record_usage(tenant_id: str, action: str, count: int = 1) -> None:
    """Record a usage event for a tenant."""
    async with async_session_maker() as db:
        record = UsageRecord(
            tenant_id=tenant_id,
            action=action,
            count=count,
        )
        db.add(record)
        await db.commit()


async def get_monthly_usage(db: AsyncSession, tenant_id: str, action: str | None = None) -> int:
    """Get total usage for the current month."""
    now = utcnow_naive()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    query = select(func.sum(UsageRecord.count)).where(
        UsageRecord.tenant_id == tenant_id,
        UsageRecord.recorded_at >= month_start,
    )
    if action:
        query = query.where(UsageRecord.action == action)

    result = await db.execute(query)
    return result.scalar_one() or 0


async def check_usage_limit(db: AsyncSession, tenant: Tenant, action: str, increment: int = 1) -> bool:
    """Check if tenant has remaining usage for the given action.

    Returns True if allowed, False if limit exceeded.
    """
    monthly = await get_monthly_usage(db, tenant.id, action)

    limits = {
        "broadcast": tenant.monthly_message_limit,
        "auto_reply": tenant.monthly_auto_reply_limit,
        "reply_macro": tenant.monthly_message_limit,
        "api_call": tenant.monthly_message_limit * 10,
    }

    limit = limits.get(action, 100)
    return (monthly + increment) <= limit


# ─── Tenant Plan Limits ───────────────────────────────────────────────

PLAN_LIMITS = {
    "free": get_plan_limits("free"),
    "pro": get_plan_limits("pro"),
    "team": get_plan_limits("team"),
}


async def apply_plan_limits(db: AsyncSession, tenant: Tenant, plan: str) -> Tenant:
    """Apply plan limits to a tenant (called on plan change or creation).

    Validates the plan ID against the canonical PLAN_CATALOG.
    Rejects deprecated and unknown plan IDs with ValueError.
    """
    validate_plan_id(plan)
    limits = PLAN_LIMITS.get(plan, PLAN_LIMITS["free"])

    tenant.plan = plan
    tenant.max_accounts = limits["max_accounts"]
    tenant.max_auto_reply_rules = limits["max_auto_reply_rules"]
    tenant.max_reply_macros = limits["max_reply_macros"]
    tenant.monthly_message_limit = limits["monthly_message_limit"]
    tenant.monthly_auto_reply_limit = limits["monthly_auto_reply_limit"]
    tenant.monthly_ai_chat_limit = limits["monthly_ai_chat_limit"]
    tenant.cooldown_minimum_minutes = limits["cooldown_minimum_minutes"]
    tenant.can_broadcast = limits["can_broadcast"]
    tenant.can_schedule = limits["can_schedule"]
    tenant.can_attach_images = limits["can_attach_images"]
    tenant.can_export_data = limits["can_export_data"]

    await db.commit()
    await db.refresh(tenant)
    logger.info("plan_limits_applied", tenant_id=tenant.id, plan=plan)
    return tenant


# ─── Credit System (for Telegram Stars add-ons) ──────────────────────


CREDIT_PRICES = {
    "broadcast_booster": {"stars": 100, "description": "긴급 발송 부스터 (1회)"},
    "ai_comment_pack_10": {"stars": 50, "description": "AI 댓글 10회 사용권"},
    "premium_template": {"stars": 30, "description": "프리미엄 템플릿 5종"},
    "analytics_report": {"stars": 200, "description": "상세 분석 리포트 PDF"},
    "extra_account_slot": {"stars": 150, "description": "추가 계정 슬롯 1개 (월)"},
}


async def add_stars_credit(tenant_id: str, stars_amount: int) -> dict:
    """Add Telegram Stars to a tenant's wallet (after successful payment)."""
    async with async_session_maker() as db:
        tenant = await db.get(Tenant, tenant_id)
        if not tenant:
            return {"success": False, "error": "Tenant not found"}

        tenant.stars_balance = (tenant.stars_balance or 0) + stars_amount
        await db.commit()

        logger.info("stars_credited", tenant_id=tenant_id, amount=stars_amount)
        return {"success": True, "new_balance": tenant.stars_balance}


async def spend_stars(tenant_id: str, stars_amount: int, item: str) -> dict:
    """Spend Telegram Stars on an add-on item."""
    async with async_session_maker() as db:
        tenant = await db.get(Tenant, tenant_id)
        if not tenant:
            return {"success": False, "error": "사용자를 찾을 수 없습니다."}
        if (tenant.stars_balance or 0) < stars_amount:
            return {"success": False, "error": "Stars 잔액이 부족합니다."}

        tenant.stars_balance -= stars_amount
        await db.commit()

        logger.info("stars_spent", tenant_id=tenant_id, amount=stars_amount, item=item)
        return {"success": True, "new_balance": tenant.stars_balance}
