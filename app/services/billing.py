"""Billing / 결제 서비스.

Simplified for USDT (crypto) + Telegram Stars only.
No Stripe, no PortOne — just crypto and native TG payments.

Plan pricing and limits are sourced from app.core.plans (PLAN_CATALOG).
"""

import os
from datetime import datetime, timedelta, timezone
from typing import Literal

from app.core.logging import get_logger
from app.core.plans import (
    PLAN_CATALOG,
    get_plan,
    get_plan_price_usdt,
    get_plan_limits,
    is_deprecated_plan,
)
from app.crud import user as user_crud
from app.database import async_session_maker
from app.models.api_key import APIKey
from app.models.tenant import PaymentRecord, Tenant
from app.services.usage_tracker import apply_plan_limits

logger = get_logger(__name__)


def utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ─── USDT Wallet ──────────────────────────────────────────────────────

USDT_WALLET_ADDRESS = os.getenv("USDT_WALLET_ADDRESS", "0x0000000000000000000000000000000000000000")
USDT_NETWORK = os.getenv("USDT_NETWORK", "TRC20")


# ─── Plan Prices (derived from canonical PLAN_CATALOG) ──────────────


def get_plan_prices_usdt() -> dict:
    prices = {}
    for pid, pdef in PLAN_CATALOG.items():
        prices[pid] = pdef["prices_usdt"]
    return prices


# ─── Stars Add-on Prices ──────────────────────────────────────────────

STARS_PRICES = {
    "broadcast_booster": 100,
    "ai_comment_pack_10": 50,
    "premium_template": 30,
    "analytics_report": 200,
    "extra_account_slot": 150,
    "ai_chat_pack_50": 50,
}

STARS_DESCRIPTIONS = {
    "broadcast_booster": "⚡ 긴급 발송 부스터 (1회)",
    "ai_comment_pack_10": "🤖 AI 댓글 10회 사용권",
    "premium_template": "🎨 프리미엄 템플릿 5종",
    "analytics_report": "📊 상세 분석 리포트 PDF",
    "extra_account_slot": "🔌 추가 계정 슬롯 1개 (월)",
    "ai_chat_pack_50": "🤖 AI Chat 50회 추가 사용권",
}

# AI Chat credits actually granted per pack — kept separate from STARS_PRICES so the
# Stars price and the credit amount can be tuned independently.
AI_CHAT_PACK_CREDITS = {
    "ai_chat_pack_50": 50,
}


# ─── USDT Payment ─────────────────────────────────────────────────────


async def create_usdt_invoice(tenant_id: str, plan: str, billing: Literal["monthly", "quarterly"] = "monthly") -> dict:
    """Create a USDT payment invoice: shows wallet address and amount."""
    if is_deprecated_plan(plan):
        return {"success": False, "error": "해당 요금제는 더 이상 제공되지 않습니다. Pro 또는 Team을 선택해주세요."}
    amount = get_plan_price_usdt(plan, billing)
    if amount is None:
        return {"success": False, "error": "유효하지 않은 요금제입니다."}

    label = f"TeleMon {plan.capitalize()} {'Quarterly' if billing == 'quarterly' else 'Monthly'}"
    payment_ref = f"USDT-{tenant_id[:8]}-{utcnow_naive().strftime('%Y%m%d%H%M%S')}"

    async with async_session_maker() as db:
        tenant = await db.get(Tenant, tenant_id)
        if tenant:
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
    """Confirm USDT payment received (admin-only manual override for payments the
    automated watcher couldn't auto-match, e.g. a missing/garbled memo).

    Verifies tx_hash against our wallet's actual Trongrid transaction history before
    activating anything — an admin can no longer activate a tenant with a fabricated
    tx_hash. Records a PaymentRecord keyed on tx_id so the same transaction can't be
    reused for a second tenant, and so the automated watcher won't double-process it.
    """
    from sqlalchemy import select

    from app.services.usdt_watcher import get_usdt_transactions

    async with async_session_maker() as db:
        tenant = await db.get(Tenant, tenant_id)
        if not tenant:
            return {"success": False, "error": "사용자를 찾을 수 없습니다."}

        existing = await db.execute(select(PaymentRecord).where(PaymentRecord.tx_id == tx_hash))
        if existing.scalar_one_or_none():
            return {"success": False, "error": "이미 처리된 거래입니다."}

        matched_tx = next(
            (tx for tx in await get_usdt_transactions() if tx["tx_id"] == tx_hash),
            None,
        )
        if matched_tx is None:
            return {"success": False, "error": "해당 tx_hash를 지갑 입금 내역에서 확인할 수 없습니다."}

        tenant.subscription_status = "active"
        tenant.trial_expires_at = None  # trial ends when paid plan starts
        tenant.billing_period_start = utcnow_naive()
        plan_def = get_plan(tenant.plan)
        if plan_def and "quarterly" in plan_def.get("prices_usdt", {}):
            days = 90
        else:
            days = 30
        tenant.billing_period_end = utcnow_naive() + timedelta(days=days)
        await apply_plan_limits(db, tenant, tenant.plan)

        # Issue API key for this tenant (matches the auto-watcher pattern)
        from app.core.security import generate_user_api_key, hash_api_key
        raw_key = generate_user_api_key()
        api_key = APIKey(
            key=raw_key,
            name=f"USDT-{tenant.plan}-admin-confirm",
            is_active=True,
            tenant_id=tenant.id,
        )
        db.add(api_key)
        await db.flush()

        # Also set api_key_hash for login-with-api-key compatibility
        if tenant.phone and not tenant.phone.startswith("pending-"):
            user = await user_crud.get_user_by_phone(db, tenant.phone)
            if user is not None and user.api_key_hash is None:
                user.api_key_hash = hash_api_key(raw_key)
                await db.flush()

        db.add(PaymentRecord(
            tx_id=tx_hash,
            tenant_id=tenant.id,
            from_address=matched_tx["from_address"],
            amount_usdt=matched_tx["amount_cents"],
            plan=tenant.plan,
            status="completed",
            api_key_id=api_key.id,
            block_timestamp=matched_tx["block_timestamp"],
        ))
        await db.commit()

        logger.info("usdt_payment_confirmed", tenant_id=tenant_id, plan=tenant.plan, tx_hash=tx_hash)

        from app.services.usdt_watcher import notify_payment_activated
        await notify_payment_activated(tenant.phone, tenant.plan, tenant.billing_period_end, raw_key)

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
        "currency": "XTR",
        "instructions": f"Telegram Stars {stars_amount}개로 {description}을(를) 구매합니다.",
    }


async def process_stars_payment(tenant_id: str, item: str, stars_amount: int) -> dict:
    """Process a Stars payment (called after successful TG Stars payment callback)."""
    async with async_session_maker() as db:
        tenant = await db.get(Tenant, tenant_id)
        if not tenant:
            return {"success": False, "error": "사용자를 찾을 수 없습니다."}

        if (tenant.stars_balance or 0) < stars_amount:
            return {"success": False, "error": "Stars 잔액이 부족합니다."}

        tenant.stars_balance -= stars_amount
        if item in AI_CHAT_PACK_CREDITS:
            tenant.ai_chat_credit_balance = (tenant.ai_chat_credit_balance or 0) + AI_CHAT_PACK_CREDITS[item]
        await db.commit()

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
        "ai_chat_pack_50": "AI Chat 50회 추가 사용권이 적립되었습니다.",
    }
    return benefits.get(item, "구매가 완료되었습니다.")


# ─── Add-on Listing ──────────────────────────────────────────────────


def get_all_addons() -> list[dict]:
    return [
        {"id": item_id, "name": STARS_DESCRIPTIONS[item_id], "stars_price": stars}
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
            "trial_expires_at": tenant.trial_expires_at.isoformat() if tenant.trial_expires_at else None,
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


async def downgrade_expired_tenants() -> dict:
    """Scheduled job: revert any paid-plan or expired-free-trial tenant whose access
    period has ended back to free-tier limits or blocked state.
    
    Paid tenants (plan != "free"): reverts to free-tier limits when billing_period_end
    passes. Free-trial tenants (plan == "free"): blocks access when trial_expires_at
    passes, keeping the tenant row in "expired" state for audit but revoking plan
    entitlements.
    """
    from sqlalchemy import select

    now = utcnow_naive()
    downgraded: list[str] = []

    async with async_session_maker() as db:
        # Paid tenants with expired billing period
        result = await db.execute(
            select(Tenant).where(
                Tenant.plan != "free",
                Tenant.billing_period_end.is_not(None),
                Tenant.billing_period_end < now,
            )
        )
        tenants = result.scalars().all()

        for tenant in tenants:
            previous_plan = tenant.plan
            if tenant.subscription_status != "canceled":
                tenant.subscription_status = "expired"
            await apply_plan_limits(db, tenant, "free")
            downgraded.append(tenant.id)
            logger.info("tenant_plan_expired_downgraded", tenant_id=tenant.id, previous_plan=previous_plan)

        # Free-trial tenants with expired trial
        result = await db.execute(
            select(Tenant).where(
                Tenant.plan == "free",
                Tenant.subscription_status == "active",
                Tenant.trial_expires_at.is_not(None),
                Tenant.trial_expires_at < now,
            )
        )
        expired_trials = result.scalars().all()

        for tenant in expired_trials:
            tenant.subscription_status = "expired"
            await apply_plan_limits(db, tenant, "free")
            downgraded.append(tenant.id)
            logger.info("free_trial_expired", tenant_id=tenant.id)

    return {"downgraded": len(downgraded), "tenant_ids": downgraded}


async def notify_expiring_trials() -> dict:
    """Scheduled job: send a D-1 re-engagement DM to free-trial tenants whose
    trial expires within the next 24 hours, with an upgrade CTA.

    Only reaches tenants with a resolvable Telegram chat id (bot-originated
    `tg_<id>` identity) — silently skipped for phone-based tenants, same
    constraint as usdt_watcher.notify_payment_activated. Guarded by
    trial_expiry_notified so a tenant is only ever DMed once per trial.
    """
    from sqlalchemy import select

    from app.core.telegram_identity import parse_tg_identifier
    from app.services.telegram_notify import send_telegram_message

    now = utcnow_naive()
    window_end = now + timedelta(hours=24)
    notified: list[str] = []

    async with async_session_maker() as db:
        result = await db.execute(
            select(Tenant).where(
                Tenant.plan == "free",
                Tenant.subscription_status == "active",
                Tenant.trial_expiry_notified.is_(False),
                Tenant.trial_expires_at.is_not(None),
                Tenant.trial_expires_at >= now,
                Tenant.trial_expires_at < window_end,
            )
        )
        tenants = result.scalars().all()

        for tenant in tenants:
            chat_id = parse_tg_identifier(tenant.phone)
            if chat_id is None:
                tenant.trial_expiry_notified = True
                continue

            text = (
                "⏰ 무료 체험이 내일 종료됩니다!\n\n"
                "체험이 끝나면 발송/자동응답 기능이 제한돼요. "
                "지금 업그레이드하고 끊김 없이 계속 사용해보세요.\n\n"
                "메뉴에서 \"요금제\"를 눌러 업그레이드할 수 있습니다."
            )
            sent = await send_telegram_message(chat_id, text)
            tenant.trial_expiry_notified = True
            if sent:
                notified.append(tenant.id)
                logger.info("trial_expiry_notified", tenant_id=tenant.id)

        await db.commit()

    return {"notified": len(notified), "tenant_ids": notified}
