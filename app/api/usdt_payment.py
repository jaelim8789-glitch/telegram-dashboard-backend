"""USDT 결제 API — API 키 발급받기 전용."""

import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, status

from app.core.logging import get_logger
from app.database import async_session_maker
from app.models.tenant import Tenant, PaymentRecord
from app.models.api_key import APIKey
from app.services.usage_tracker import apply_plan_limits

router = APIRouter(prefix="/api/payment", tags=["payment"])
logger = get_logger(__name__)

USDT_WALLET = "TFyAKKLYH96T1NmL92Mr7vpK87EUNnkCSc"

PLANS = {
    "basic": {"usdt": 15, "label": "Basic ($15/월)"},
    "pro": {"usdt": 38, "label": "Pro ($38/월)"},
    "enterprise": {"usdt": 150, "label": "Enterprise ($150/월)"},
}


def utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@router.get("/plans")
async def get_plans():
    """요금제 목록 + 내 지갑 주소 반환"""
    return {
        "wallet_address": USDT_WALLET,
        "network": "TRC20",
        "plans": [
            {
                "id": pid,
                "name": info["label"],
                "usdt_amount": info["usdt"],
            }
            for pid, info in PLANS.items()
        ],
    }


@router.post("/request-key")
async def request_api_key(plan: str, phone: str = ""):
    """API 키 발급 요청 → USDT 송금 정보 + payment_ref 반환
    
    1. 사용자가 요금제 선택
    2. 서버가 payment_ref(메모) 생성
    3. 사용자에게 "이 주소로 X USDT 보내세요" 표시
    4. 입금 확인되면 자동으로 API 키 발급
    """
    if plan not in PLANS:
        raise HTTPException(status_code=400, detail="유효하지 않은 요금제입니다.")

    plan_info = PLANS[plan]
    payment_ref = f"TM-{secrets.token_hex(4).upper()}"

    async with async_session_maker() as db:
        # Create or find tenant
        from sqlalchemy import select
        result = await db.execute(select(Tenant).where(Tenant.phone == phone))
        tenant = result.scalar_one_or_none()

        if not tenant:
            tenant = Tenant(
                phone=phone or f"pending-{payment_ref}",
                plan=plan,
                subscription_status="pending",
                payment_ref=payment_ref,
            )
            db.add(tenant)
        else:
            tenant.plan = plan
            tenant.subscription_status = "pending"
            tenant.payment_ref = payment_ref

        await db.commit()

    return {
        "success": True,
        "payment_ref": payment_ref,
        "wallet_address": USDT_WALLET,
        "network": "TRC20",
        "amount_usdt": plan_info["usdt"],
        "plan": plan,
        "plan_name": plan_info["label"],
        "instructions": (
            f"1. 위 {USDT_WALLET} 주소로 **{plan_info['usdt']} USDT(TRC20)**를 보내주세요.\n"
            f"2. 송금 시 메모(memo)에 반드시 `{payment_ref}`를 입력하세요.\n"
            f"3. 입금 확인 후 자동으로 API 키가 발급됩니다.\n"
            f"4. 평균 처리 시간: 5~10분"
        ),
    }


@router.get("/status/{payment_ref}")
async def check_payment_status(payment_ref: str):
    """결제 상태 확인 (API 키가 발급되었는지)"""
    async with async_session_maker() as db:
        from sqlalchemy import select

        # Find payment record
        result = await db.execute(
            select(PaymentRecord).where(PaymentRecord.tx_id.ilike(f"%{payment_ref}%"))
        )
        payment = result.scalar_one_or_none()

        if not payment:
            # Check if tenant exists with this ref
            result = await db.execute(
                select(Tenant).where(Tenant.payment_ref == payment_ref)
            )
            tenant = result.scalar_one_or_none()
            if tenant and tenant.subscription_status == "active":
                # Find the API key
                key_result = await db.execute(
                    select(APIKey).where(APIKey.name.ilike(f"%{tenant.plan}%"))
                )
                api_key = key_result.scalar_one_or_none()
                return {
                    "status": "completed",
                    "api_key": api_key.key if api_key else None,
                    "plan": tenant.plan,
                }
            return {"status": "pending", "message": "입금을 기다리는 중입니다..."}

        if payment.status == "completed":
            api_key = await db.get(APIKey, payment.api_key_id)
            return {
                "status": "completed",
                "api_key": api_key.key if api_key else None,
                "plan": payment.plan,
                "tx_id": payment.tx_id,
            }

        return {"status": payment.status, "message": "처리 중입니다..."}