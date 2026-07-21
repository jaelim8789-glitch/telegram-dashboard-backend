"""
Cryptomus crypto payment integration — invoice creation, webhook processing,
and payment tracking.

Isolated from the existing USDT/TronGrid monitoring path.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.plans import PLAN_CATALOG, validate_plan_id
from app.core.security import generate_user_api_key, hash_api_key
from app.database import async_session_maker
from app.models.api_key import APIKey
from app.models.cryptomus_payment import CryptomusPayment
from app.models.tenant import Tenant
from app.models.user import User
from app.services.usage_tracker import apply_plan_limits

logger = get_logger(__name__)

CRYPTOMUS_API_BASE = "https://api.cryptomus.com/v1"


def get_cryptomus_config() -> dict[str, str]:
    return {
        "api_key": os.environ.get("CRYPTOMUS_API_KEY", ""),
        "merchant_id": os.environ.get("CRYPTOMUS_MERCHANT_ID", ""),
        "webhook_secret": os.environ.get("CRYPTOMUS_WEBHOOK_SECRET", ""),
    }


def verify_webhook_signature(secret: str, body: bytes, sign: str) -> bool:
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sign)


async def create_cryptomus_invoice(
    amount: str,
    currency: str,
    network: str,
    order_id: str,
    lifetime: int = 1800,
) -> dict[str, Any]:
    cfg = get_cryptomus_config()
    if not cfg["api_key"] or not cfg["merchant_id"]:
        raise RuntimeError("Cryptomus credentials not configured")

    payload = {
        "amount": amount,
        "currency": currency,
        "network": network,
        "order_id": order_id,
        "lifetime": lifetime,
        "is_amount_editable": False,
    }
    body_str = json.dumps(payload, separators=(",", ":"))
    sign = hmac.new(
        cfg["api_key"].encode(), body_str.encode(), hashlib.sha256
    ).hexdigest()

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{CRYPTOMUS_API_BASE}/payments",
            content=body_str,
            headers={
                "merchant": cfg["merchant_id"],
                "sign": sign,
                "Content-Type": "application/json",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        result = data.get("result") or {}
        if not result.get("uuid"):
            raise RuntimeError(f"Invalid Cryptomus response: {data}")
        return result


async def create_payment_record(
    db: AsyncSession,
    tenant_id: str | None,
    invoice_id: str,
    order_id: str,
    plan: str,
    network: str,
    amount_usd: float,
    currency: str,
    payment_address: str | None = None,
    qr_code_url: str | None = None,
    expires_at: str | None = None,
) -> CryptomusPayment:
    payment = CryptomusPayment(
        tenant_id=tenant_id,
        invoice_id=invoice_id,
        order_id=order_id,
        plan=plan,
        network=network,
        amount_usd=amount_usd,
        currency=currency,
        status="pending",
        payment_address=payment_address,
        qr_code_url=qr_code_url,
        expires_at=expires_at,
        created_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.add(payment)
    await db.commit()
    await db.refresh(payment)
    return payment


async def get_payment(db: AsyncSession, invoice_id: str) -> CryptomusPayment | None:
    result = await db.execute(
        select(CryptomusPayment).where(CryptomusPayment.invoice_id == invoice_id)
    )
    return result.scalar_one_or_none()


async def mark_payment_paid(
    db: AsyncSession,
    payment: CryptomusPayment,
    paid_amount: str,
    paid_currency: str,
    api_key: str | None = None,
) -> None:
    payment.status = "paid"
    payment.paid_amount = paid_amount
    payment.paid_currency = paid_currency
    if api_key and not payment.issued_api_key:
        payment.issued_api_key = api_key
    payment.processed_at = datetime.now(timezone.utc).isoformat()
    payment.webhook_received_at = datetime.now(timezone.utc).isoformat()
    await db.commit()
    await db.refresh(payment)


async def mark_payment_failed(db: AsyncSession, payment: CryptomusPayment, status: str) -> None:
    payment.status = status
    await db.commit()
    await db.refresh(payment)


async def mark_payment_amount_mismatch(db: AsyncSession, payment: CryptomusPayment) -> None:
    payment.status = "amount_mismatch"
    await db.commit()
    await db.refresh(payment)


async def activate_tenant_plan(db: AsyncSession, tenant_id: str, plan: str) -> dict[str, Any]:
    """Activate a tenant's plan after successful Cryptomus payment.

    Reuses the existing plan activation + API key issuance logic.
    """
    validate_plan_id(plan)

    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        return {"success": False, "error": "Tenant not found"}

    tenant.subscription_status = "active"
    tenant.trial_expires_at = None
    tenant.billing_period_start = datetime.now(timezone.utc).replace(tzinfo=None)
    plan_def = PLAN_CATALOG.get(plan, {})
    if "quarterly" in plan_def.get("prices_usdt", {}):
        days = 90
    else:
        days = 30
    tenant.billing_period_end = datetime.now(timezone.utc).replace(tzinfo=None) + __import__("datetime").timedelta(days=days)

    await apply_plan_limits(db, tenant, plan)

    raw_key = generate_user_api_key()
    api_key = APIKey(
        key=raw_key,
        name=f"Cryptomus-{plan}",
        is_active=True,
        tenant_id=tenant.id,
        purpose="payment_issued",
    )
    db.add(api_key)
    await db.flush()

    if tenant.phone and not tenant.phone.startswith("pending-"):
        result = await db.execute(select(User).where(User.phone == tenant.phone))
        user = result.scalar_one_or_none()
        if user is not None and user.api_key_hash is None:
            user.api_key_hash = hash_api_key(raw_key)
            await db.flush()

    await db.commit()
    await db.refresh(tenant)

    return {
        "success": True,
        "api_key": raw_key,
        "tenant_id": tenant.id,
        "plan": plan,
    }
