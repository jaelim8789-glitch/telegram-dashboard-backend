"""
Cryptomus crypto payment API — isolated from existing USDT monitoring.

Endpoints:
  POST /api/payments/crypto/create-invoice — create Cryptomus invoice
  POST /api/payments/crypto/webhook        — receive Cryptomus webhook
  GET  /api/payments/crypto/status/{invoice_id} — poll payment status
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_identity, Identity, require_admin
from app.core.logging import get_logger
from app.core.plans import PLAN_CATALOG, validate_plan_id
from app.database import get_db
from app.models.cryptomus_payment import CryptomusPayment
from app.services.cryptomus import (
    activate_tenant_plan,
    create_cryptomus_invoice,
    create_payment_record,
    get_cryptomus_config,
    get_payment,
    mark_payment_amount_mismatch,
    mark_payment_failed,
    mark_payment_paid,
    verify_webhook_signature,
)

logger = get_logger(__name__)
router = APIRouter(prefix="/api/payments/crypto", tags=["crypto-payments"])

ALLOWED_NETWORKS = {"TRC20", "BEP20", "ERC20", "SOL"}


@router.post("/create-invoice")
async def create_invoice(
    body: dict[str, Any],
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    plan = str(body.get("plan", "")).lower()
    network = str(body.get("network", "TRC20")).upper()

    try:
        validate_plan_id(plan)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if plan == "free":
        raise HTTPException(
            status_code=400, detail="FREE plan does not require payment"
        )

    if network not in ALLOWED_NETWORKS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid network. Allowed: {', '.join(sorted(ALLOWED_NETWORKS))}",
        )

    plan_def = PLAN_CATALOG.get(plan, {})
    prices = plan_def.get("prices_usdt", {})
    billing = "monthly" if "monthly" in prices else "quarterly"
    amount_usd = prices.get(billing, 0)
    currency = "USDT"

    if amount_usd <= 0:
        raise HTTPException(
            status_code=400, detail="Plan not available for crypto payment"
        )

    order_id = str(__import__("uuid").uuid4())
    try:
        result = await create_cryptomus_invoice(
            amount=str(amount_usd),
            currency=currency,
            network=network,
            order_id=order_id,
        )
    except RuntimeError as exc:
        logger.error("[cryptomus] create invoice failed: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        logger.error("[cryptomus] create invoice unexpected error: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to create invoice")

    invoice_id = result.get("uuid", "")
    tenant_id = identity.tenant_id if identity.tenant_id else None

    await create_payment_record(
        db=db,
        tenant_id=tenant_id,
        invoice_id=invoice_id,
        order_id=order_id,
        plan=plan,
        network=network,
        amount_usd=amount_usd,
        currency=currency,
        payment_address=result.get("address"),
        qr_code_url=result.get("qr_code"),
        expires_at=result.get("expired_at"),
    )

    logger.info(
        "[cryptomus] invoice created: tenant=%s plan=%s network=%s invoice=%s",
        tenant_id,
        plan,
        network,
        invoice_id,
    )

    return {
        "ok": True,
        "invoice_id": invoice_id,
        "order_id": order_id,
        "plan": plan,
        "network": network,
        "amount_usd": amount_usd,
        "currency": currency,
        "payment_address": result.get("address"),
        "qr_code_url": result.get("qr_code"),
        "expires_at": result.get("expired_at"),
    }


@router.post("/webhook")
async def cryptomus_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    raw_body = await request.body()
    sign = request.headers.get("sign", "")
    cfg = get_cryptomus_config()

    if not verify_webhook_signature(cfg["webhook_secret"], raw_body, sign):
        logger.warning("[cryptomus] invalid webhook signature")
        return JSONResponse(
            content={"ok": False, "error": "invalid signature"}, status_code=200
        )

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        logger.error("[cryptomus] invalid webhook JSON")
        return JSONResponse(
            content={"ok": False, "error": "invalid json"}, status_code=200
        )

    invoice_id = str(payload.get("uuid", ""))
    status = str(payload.get("status", "")).lower()
    payment_amount = payload.get("payment_amount", "")
    payment_currency = payload.get("payment_currency", "")

    if not invoice_id:
        return JSONResponse(content={"ok": False}, status_code=200)

    payment = await get_payment(db, invoice_id)
    if not payment:
        logger.warning("[cryptomus] webhook for unknown invoice: %s", invoice_id)
        return JSONResponse(
            content={"ok": False, "error": "unknown invoice"}, status_code=200
        )

    if payment.status != "pending":
        logger.info(
            "[cryptomus] duplicate webhook ignored: %s status=%s",
            invoice_id,
            payment.status,
        )
        return JSONResponse(content={"ok": True, "duplicate": True}, status_code=200)

    if status == "paid":
        try:
            expected = float(payment.amount_usd) if payment.amount_usd is not None else 0.0
            actual = float(payment_amount) if payment_amount else 0.0
            if abs(actual - expected) > 0.01:
                logger.error(
                    "[cryptomus] amount mismatch: invoice=%s expected=%s actual=%s",
                    invoice_id,
                    expected,
                    actual,
                )
                await mark_payment_amount_mismatch(db, payment)
                return JSONResponse(
                    content={"ok": True, "error": "amount_mismatch"}, status_code=200
                )
        except (ValueError, TypeError):
            pass

        if not payment.tenant_id:
            logger.error("[cryptomus] payment has no tenant_id: %s", invoice_id)
            await mark_payment_failed(db, payment, "error")
            return JSONResponse(
                content={"ok": False, "error": "no tenant"}, status_code=200
            )

        activation = await activate_tenant_plan(db, payment.tenant_id, payment.plan)
        api_key_raw = activation.get("api_key")

        await mark_payment_paid(
            db,
            payment,
            str(payment_amount),
            payment_currency or "",
            api_key_raw,
        )
        logger.info(
            "[cryptomus] payment confirmed: tenant=%s plan=%s amount=%s invoice=%s",
            payment.tenant_id,
            payment.plan,
            payment_amount,
            invoice_id,
        )
    elif status in ("failed", "expired", "wrong_amount"):
        await mark_payment_failed(db, payment, status)
        logger.info("[cryptomus] payment %s: invoice=%s", status, invoice_id)

    return JSONResponse(content={"ok": True}, status_code=200)


@router.get("/status/{invoice_id}")
async def payment_status(
    invoice_id: str,
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    payment = await get_payment(db, invoice_id)
    if not payment:
        raise HTTPException(status_code=404, detail="Invoice not found")

    if payment.tenant_id and payment.tenant_id != identity.tenant_id:
        if identity.kind != "admin":
            raise HTTPException(status_code=403, detail="Forbidden")

    return {
        "ok": True,
        "invoice_id": payment.invoice_id,
        "order_id": payment.order_id,
        "status": payment.status,
        "plan": payment.plan,
        "network": payment.network,
        "amount_usd": payment.amount_usd,
        "currency": payment.currency,
        "payment_address": payment.payment_address,
        "qr_code_url": payment.qr_code_url,
        "expires_at": payment.expires_at,
        "paid_amount": payment.paid_amount,
        "paid_currency": payment.paid_currency,
        "api_key": payment.issued_api_key,
    }
