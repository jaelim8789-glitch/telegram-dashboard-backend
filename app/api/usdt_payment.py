"""USDT 결제 API — API 키 발급받기 전용.

Trust boundary:
  /plans           — intentionally public (read-only plan info)
  /request-key     — intentionally public (user hasn't paid yet), rate-limited
  /status/{ref}    — intentionally public, returns masked API key only

Payment verification is done server-side by usdt_watcher.py (scheduled task)
which queries Trongrid directly — client-supplied tx data is NOT trusted here.
"""

import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, status

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

# Simple in-memory rate limit: {(phone_or_ip, endpoint): timestamp}
_request_timestamps: dict[tuple[str, str], float] = {}


def _check_rate_limit(key: tuple[str, str], min_interval: float = 10.0) -> bool:
    """Returns True if allowed, False if rate-limited."""
    import time
    now = time.time()
    last = _request_timestamps.get(key, 0.0)
    if now - last < min_interval:
        return False
    _request_timestamps[key] = now
    return True


def utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@router.get("/plans")
async def get_plans():
    """요금제 목록 + 내 지갑 주소 반환 (public)"""
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
async def request_api_key(plan: str, phone: str = "", request: Request = None):
    """API 키 발급 요청 → USDT 송금 정보 + payment_ref 반환 (public, rate-limited)

    보안 설계:
    - Rate-limited: 동일 phone/IP는 10초에 1회만 요청 가능
    - payment_ref는 서버에서 생성 (클라이언트 제공 불가)
    - 요금제는 서버에서 검증 (클라이언트 금액 신뢰 안 함)
    - 실제 입금 확인은 usdt_watcher.py (scheduled task)가 Trongrid API로 직접 검증
    """
    if plan not in PLANS:
        raise HTTPException(status_code=400, detail="유효하지 않은 요금제입니다.")

    # Rate limit by IP or phone
    rate_key = phone if phone else (request.client.host if request else "unknown")
    if not _check_rate_limit(("request-key", rate_key), min_interval=10.0):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="너무 많은 요청입니다. 10초 후 다시 시도해주세요.",
        )

    plan_info = PLANS[plan]
    payment_ref = f"TM-{secrets.token_hex(4).upper()}"

    async with async_session_maker() as db:
        from sqlalchemy import select

        # Find or create tenant — tie to phone if provided
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
            # Only update if tenant is not already active
            if tenant.subscription_status == "active":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="이미 활성화된 요금제가 있습니다. 추가 결제가 필요하시면 고객지원으로 문의해주세요.",
                )
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
    """결제 상태 확인 (public) — API 키는 마스킹해서 반환"""
    async with async_session_maker() as db:
        from sqlalchemy import select

        # Check payment record first
        result = await db.execute(
            select(PaymentRecord).where(PaymentRecord.tx_id.ilike(f"%{payment_ref}%"))
        )
        payment = result.scalar_one_or_none()

        if not payment:
            # Check tenant status
            result = await db.execute(
                select(Tenant).where(Tenant.payment_ref == payment_ref)
            )
            tenant = result.scalar_one_or_none()
            if tenant and tenant.subscription_status == "active":
                key_result = await db.execute(
                    select(APIKey).where(APIKey.name.ilike(f"%{tenant.plan}%"))
                )
                api_key = key_result.scalar_one_or_none()
                masked_key = (
                    api_key.key[:8] + "..." + api_key.key[-4:]
                    if api_key and len(api_key.key) > 12
                    else "발급 완료"
                )
                return {
                    "status": "completed",
                    "api_key_masked": masked_key,
                    "plan": tenant.plan,
                }
            return {"status": "pending", "message": "입금을 기다리는 중입니다..."}

        if payment.status == "completed":
            api_key = await db.get(APIKey, payment.api_key_id)
            masked_key = (
                api_key.key[:8] + "..." + api_key.key[-4:]
                if api_key and len(api_key.key) > 12
                else "발급 완료"
            )
            return {
                "status": "completed",
                "api_key_masked": masked_key,
                "plan": payment.plan,
                "tx_id": payment.tx_id,
            }

        return {"status": payment.status, "message": "처리 중입니다..."}