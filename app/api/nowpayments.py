"""
NOWPayments API — NOWPayments 결제 시스템 통합.

Endpoints:
  POST /api/payments/nowpayments/create-invoice — NOWPayments 인보이스 생성
  POST /api/payments/nowpayments/webhook        — NOWPayments IPN 수신
  GET  /api/payments/nowpayments/status/{payment_id} — 결제 상태 조회
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_identity, Identity, require_api_key_or_admin
from app.core.logging import get_logger
from app.core.plans import validate_plan_id
from app.database import get_db
from app.services.nowpayments import get_nowpayments_service
from app.models.nowpayments import NowPaymentsTransaction

logger = get_logger(__name__)
router = APIRouter(prefix="/api/payments/nowpayments", tags=["nowpayments-payments"])

# NOWPayments' own servers call /webhook (IPN) — it can't carry our auth headers,
# so it stays public and relies on its own signature verification instead. Every
# other endpoint here handles money/tenant data and must require auth explicitly
# (this router is NOT registered with router-level auth in main.py, precisely
# because webhook needs to stay open).
_auth_required = [Depends(require_api_key_or_admin)]


@router.post("/create-invoice", dependencies=_auth_required)
async def create_invoice(
    body: dict[str, Any],
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    """NOWPayments 인보이스 생성"""
    plan_id = str(body.get("plan", "")).lower()
    currency = str(body.get("currency", "usdt")).upper()
    
    # 플랜 유효성 검사
    if not validate_plan_id(plan_id):
        raise HTTPException(
            status_code=400, detail="Invalid plan. Use valid plan ID"
        )
    
    # 통화 유효성 검사
    allowed_currencies = {"USDT", "BTC", "ETH", "BNB", "TRX", "LTC", "DOGE", "SOL", "MATIC"}
    if currency not in allowed_currencies:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid currency. Allowed: {', '.join(sorted(allowed_currencies))}",
        )
    
    try:
        service = get_nowpayments_service()
        result = await service.create_payment(
            amount=body.get("amount", 0),
            currency=currency.lower(),
            plan_id=plan_id,
            tenant_id=identity.tenant_id,
            order_description=body.get("description", f"TeleMon {plan_id.capitalize()} Subscription")
        )
        
        return result
    except RuntimeError as exc:
        logger.error("[nowpayments] create invoice failed: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        logger.error("[nowpayments] create invoice unexpected error: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to create invoice")


@router.post("/webhook")
async def nowpayments_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """NOWPayments IPN (Instant Payment Notification) 수신"""
    # 요청 본문 가져오기
    payload = await request.body()
    
    # 서명 헤더 가져오기
    signature = request.headers.get("x-nowpayments-signature")
    if not signature:
        logger.warning("NOWPayments webhook: Missing signature header")
        raise HTTPException(status_code=400, detail="Missing signature header")
    
    # 서명 검증
    service = get_nowpayments_service()
    if not service.verify_webhook_signature(payload, signature):
        logger.warning("NOWPayments webhook: Invalid signature")
        raise HTTPException(status_code=400, detail="Invalid signature")
    
    try:
        # JSON 파싱
        webhook_data = json.loads(payload.decode("utf-8"))
        
        # 웹훅 처리
        await service.process_webhook(webhook_data, db)
        
        return {"success": True}
    except json.JSONDecodeError as e:
        logger.error("NOWPayments webhook: Invalid JSON: %s", e)
        raise HTTPException(status_code=400, detail="Invalid JSON")
    except Exception as e:
        logger.error("NOWPayments webhook processing error: %s", e)
        raise HTTPException(status_code=500, detail="Webhook processing error")


@router.get("/status/{payment_id}", dependencies=_auth_required)
async def get_payment_status(
    payment_id: str,
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db)
):
    """특정 결제 상태 조회"""
    result = await db.execute(
        select(NowPaymentsTransaction).where(
            NowPaymentsTransaction.payment_id == payment_id,
            NowPaymentsTransaction.tenant_id == identity.tenant_id
        )
    )
    transaction = result.scalar_one_or_none()
    
    if not transaction:
        raise HTTPException(status_code=404, detail="Payment not found")
    
    return {
        "payment_id": transaction.payment_id,
        "status": transaction.payment_status,
        "amount": transaction.amount,
        "paid_amount": transaction.paid_amount,
        "currency": transaction.pay_currency,
        "order_id": transaction.order_id,
        "created_at": transaction.created_at.isoformat() if transaction.created_at else None
    }


@router.get("/history", dependencies=_auth_required)
async def get_payment_history(
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db)
):
    """사용자의 결제 내역 조회"""
    result = await db.execute(
        select(NowPaymentsTransaction)
        .where(NowPaymentsTransaction.tenant_id == identity.tenant_id)
        .order_by(NowPaymentsTransaction.created_at.desc())
    )
    transactions = result.scalars().all()
    
    return [
        {
            "payment_id": t.payment_id,
            "status": t.payment_status,
            "amount": t.amount,
            "paid_amount": t.paid_amount,
            "currency": t.pay_currency,
            "order_id": t.order_id,
            "created_at": t.created_at.isoformat() if t.created_at else None
        }
        for t in transactions
    ]