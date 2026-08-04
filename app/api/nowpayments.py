"""
NOWPayments API — NOWPayments 결제 시스템 통합.

Endpoints:
  POST /api/payments/nowpayments/webhook         NOWPayments IPN 
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
from app.core.plans import validate_plan_id, get_plan
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


@router.post("/create-invoice")
async def create_invoice(
    body: dict[str, Any],
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    """NOWPayments 인보이스 생성"""
    plan_id = str(body.get("plan_id", body.get("plan", ""))).lower()
    billing = str(body.get("billing", "monthly")).lower()
    
    # 플랜 유효성 검사
    if not validate_plan_id(plan_id):
        raise HTTPException(
            status_code=400, detail="Invalid plan. Use valid plan ID"
        )
    
    # 플랜 가격 조회
    plan_info = get_plan(plan_id)
    if not plan_info:
        raise HTTPException(status_code=400, detail="Plan not found")
    
    # billing에 따른 가격 계산
    prices = plan_info.get("prices_usdt", {})
    price_usd = prices.get(billing, prices.get("monthly", 0))
    
    currency = str(body.get("currency", "usdttrc20")).upper()
    
    # 통화 유효성 검사
    allowed_currencies = {"USDTTRC20", "USDTTON", "USDT", "BTC", "ETH", "BNB", "TRX", "LTC", "DOGE", "SOL", "MATIC"}
    if currency not in allowed_currencies:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid currency. Allowed: {', '.join(sorted(allowed_currencies))}",
        )
    
    try:
        service = get_nowpayments_service()
        result = await service.create_payment(
            amount=price_usd,
            currency=currency.lower(),
            plan_id=plan_id,
            tenant_id=identity.tenant_id,
            order_description=body.get("description", f"TeleMon {plan_id.capitalize()} Subscription ({billing})")
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
        "fulfilled": transaction.fulfilled,
        "fulfilled_at": transaction.fulfilled_at.isoformat() if transaction.fulfilled_at else None,
        "created_at": transaction.created_at.isoformat() if transaction.created_at else None
    }


@router.get("/receipt/{payment_id}", dependencies=_auth_required)
async def get_payment_receipt(
    payment_id: str,
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    """PDF 영수증 다운로드 — tenant 소유권 검증 후 반환."""
    from fastapi.responses import Response

    from app.models.tenant import Tenant
    from app.services.receipt import generate_payment_receipt

    result = await db.execute(
        select(NowPaymentsTransaction).where(
            NowPaymentsTransaction.payment_id == payment_id,
            NowPaymentsTransaction.tenant_id == identity.tenant_id,
        )
    )
    transaction = result.scalar_one_or_none()
    if not transaction:
        raise HTTPException(status_code=404, detail="결제 내역을 찾을 수 없습니다.")

    tenant = await db.get(Tenant, identity.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="테넌트를 찾을 수 없습니다.")

    pdf_bytes = generate_payment_receipt(tenant, transaction)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="telemon-receipt-{transaction.payment_id}.pdf"',
        },
    )


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
            "fulfilled": t.fulfilled,
            "fulfilled_at": t.fulfilled_at.isoformat() if t.fulfilled_at else None,
            "created_at": t.created_at.isoformat() if t.created_at else None
        }
        for t in transactions
    ]