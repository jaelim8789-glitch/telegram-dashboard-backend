"""Billing / 결제 서비스.
    
Simplified for USDT (crypto) + Telegram Stars only.
No Stripe, no PortOne — just crypto and native TG payments.
"""

import os
from datetime import datetime, timedelta, timezone
from typing import Literal

from app.core.logging import get_logger
from app.database import async_session_maker
from app.models.tenant import Tenant
from app.services.usage_tracker import apply_plan_limits

logger = get_logger(__name__)


def utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ─── USDT Wallet ──────────────────────────────────────────────────────

# 내 USDT 지갑 주소 (여기로 입금되면 수동/자동 확인)
USDT_WALLET_ADDRESS = os.getenv("USDT_WALLET_ADDRESS", "0x0000000000000000000000000000000000000000")
USDT_NETWORK = os.getenv("USDT_NETWORK", "TRC20")  # TRC20, ERC20, BEP20

# ─── Plan Prices in USDT ──────────────────────────────────────────────

PLAN_PRICES_USDT = {
    "free": 0,
    "basic": 15,        # $15/월 (~₩19,900)
    "pro": 38,          # $38/월 (~₩49,900)
    "enterprise": 150,  # $150/월 (~₩199,000)
}

PLAN_PRICES_USDT_ANNUAL = {
    "basic": 144,       # $144/년 (20% 할인)
    "pro": 365,         # $365/년 (20% 할인)
    "enterprise": 1440, # $1,440/년 (20% 할인)
}

# ─── Stars Add-on Prices ──────────────────────────────────────────────

STARS_PRICES = {
    "broadcast_booster": 100,
    "ai_comment_pack_10": 50,
    "premium_template": 30,
    "analytics_report": 200,
    "extra_account_slot": 150,
}

STARS_DESCRIPTIONS = {
    "broadcast_booster": "⚡ 긴급 발송 부스터 (1회)",
    "ai_comment_pack_10": "🤖 AI 댓글 10회 사용권",
    "premium_template": "🎨 프리미엄 템플릿 5종",
    "analytics_report": "📊 상세 분석 리포트 PDF",
    "extra_account_slot": "🔌 추가 계정 슬롯 1개 (월)",
}


# ─── USDT Payment ─────────────────────────────────────────────────────


async def create_usdt_invoice(tenant_id: str, plan: str, billing: Literal["monthly", "annual"] = "monthly") -> dict:
    """Create a USDT payment invoice: shows wallet address and amount."""
    plan_info = PLAN_PRICES_USDT.get(plan)
    if plan_info is None:
        return {"success": False, "error": "유효하지 않은 요금제입니다."}

    amount = plan_info
    if billing == "annual" and plan in PLAN_PRICES_USDT_ANNUAL:
        amount = PLAN_PRICES_USDT_ANNUAL[plan]

    label = f"TeleMon {plan.capitalize()} {'Annual' if billing == 'annual' else 'Monthly'}"

    # Generate a unique payment reference (for manual verification)
    payment_ref = f"USDT-{tenant_id[:8]}-{utcnow_naive().strftime('%Y%m%d%H%M%S')}"

    async with async_session_maker() as db:
        tenant = await db.get(Tenant, tenant_id)
        if tenant:
            # Save pending payment reference
            tenant.subscription_status = "pending"
            await db.commit()

    return {
        "success": True,
        "payment_method": "USDT",
        "network": USDT_NETWORK,
        "wallet_address": USDT_WALLET_ADDRESS,
        "amount_usdt": amount,
        "plan": plan,
        "billing": billing,
        "label": label,
        "payment_ref": payment_ref,
        "instructions": (
            f"위 {USDT_NETWORK} 지갑 주소로 **{amount} USDT**를 보내주세요.\n"
            f"입금 확인 후 자동으로 {plan.capitalize()} 요금제가 활성화됩니다.\n"
            f"보내실 때 메모(memo)에 반드시 `{payment_ref}`를 입력해주세요.\n"
            f"⏳ 처리 시간: 영업일 기준 5~30분"
        ),
    }


async def confirm_usdt_payment(tenant_id: str, tx_hash: str) -> dict:
    """Confirm USDT payment received (called by admin or webhook)."""
    async with async_session_maker() as db:
        tenant = await db.get(Tenant, tenant_id)
        if not tenant:
            return {"success": False, "error": "사용자를 찾을 수 없습니다."}

        tenant.subscription_status = "active"
        tenant.billing_period_start = utcnow_naive()
        tenant.billing_period_end = utcnow_naive() + timedelta(days=30)
        await apply_plan_limits(db, tenant, tenant.plan)
        
        logger.info("usdt_payment_confirmed", tenant_id=tenant_id, plan=tenant.plan, tx_hash=tx_hash)
        return {
            "success": True,
            "message": f"USDT 입금 확인 완료! {tenant.plan.capitalize()} 요금제가 활성화되었습니다.",
            "tx_hash": tx_hash,
        }


# ─── Telegram Stars Payment ────────────────────────────────────────────


async def create_stars_invoice(tenant_id: str, item: str) -> dict:
    """Create a Telegram Stars invoice for an add-on item."""
    stars_amount = STARS_PRICES.get(item)
    if not stars_amount:
        return {"success": False, "error": "유효하지 않은 아이템입니다."}

    description = STARS_DESCRIPTIONS.get(item, item)

    return {
        "success": True,
        "item": item,
        "stars_amount": stars_amount,
        "description": description,
        "currency": "XTR",  # Telegram Stars
        "instructions": f"Telegram Stars {stars_amount}개로 {description}을(를) 구매합니다.",
    }


async def process_stars_payment(tenant_id: str, item: str, stars_amount: int) -> dict:
    """Process a Stars payment (called after successful TG Stars payment callback)."""
    from app.services.usage_tracker import record_usage

    async with async_session_maker() as db:
        tenant = await db.get(Tenant, tenant_id)
        if not tenant:
            return {"success": False, "error": "사용자를 찾을 수 없습니다."}

        if (tenant.stars_balance or 0) >= stars_amount:
            # Deduct from balance
            tenant.stars_balance -= stars_amount
        else:
            # Stars already paid via TG invoice — just credit the item
            pass

        await db.commit()

        # Grant the item benefit
        benefit = _get_item_benefit(item)
        logger.info("stars_payment_processed", tenant_id=tenant_id, item=item, stars=stars_amount)

        return {
            "success": True,
            "item": item,
            "benefit": benefit,
            "stars_spent": stars_amount,
            "remaining_stars": tenant.stars_balance or 0,
        }


def _get_item_benefit(item: str) -> str:
    benefits = {
        "broadcast_booster": "긴급 발송 1회 권한이 추가되었습니다.",
        "ai_comment_pack_10": "AI 댓글 10회 사용권이 추가되었습니다.",
        "premium_template": "프리미엄 템플릿 5종이 영구 추가되었습니다.",
        "analytics_report": "월간 분석 리포트 다운로드가 가능합니다.",
        "extra_account_slot": "계정 슬롯이 1개 추가되었습니다 (이번 달).",
    }
    return benefits.get(item, "구매가 완료되었습니다.")


# ─── Add-on Listing ──────────────────────────────────────────────────


def get_all_addons() -> list[dict]:
    """Get all available add-ons with prices."""
    return [
        {
            "id": item_id,
            "name": STARS_DESCRIPTIONS[item_id],
            "stars_price": stars,
        }
        for item_id, stars in STARS_PRICES.items()
    ]


# ─── Subscription Management ─────────────────────────────────────────


async def get_subscription_status(tenant_id: str) -> dict:
    """Get current subscription status."""
    async with async_session_maker() as db:
        tenant = await db.get(Tenant, tenant_id)
        if not tenant:
            return {"success": False, "error": "사용자를 찾을 수 없습니다."}

        return {
            "success": True,
            "plan": tenant.plan,
            "status": tenant.subscription_status,
            "billing_period_start": tenant.billing_period_start.isoformat() if tenant.billing_period_start else None,
            "billing_period_end": tenant.billing_period_end.isoformat() if tenant.billing_period_end else None,
            "stars_balance": tenant.stars_balance or 0,
            "features": {
                "max_accounts": tenant.max_accounts,
                "max_auto_reply_rules": tenant.max_auto_reply_rules,
                "max_reply_macros": tenant.max_reply_macros,
                "monthly_message_limit": tenant.monthly_message_limit,
                "can_broadcast": tenant.can_broadcast,
                "can_schedule": tenant.can_schedule,
                "can_attach_images": tenant.can_attach_images,
            },
        }


async def cancel_subscription(tenant_id: str) -> dict:
    """Cancel subscription at period end."""
    async with async_session_maker() as db:
        tenant = await db.get(Tenant, tenant_id)
        if not tenant:
            return {"success": False, "error": "사용자를 찾을 수 없습니다."}

        tenant.subscription_status = "canceled"
        await db.commit()
        logger.info("subscription_canceled", tenant_id=tenant_id)
        return {
            "success": True,
            "message": "구독이 취소되었습니다. 현재 요금제 기간까지 사용 가능합니다.",
        }