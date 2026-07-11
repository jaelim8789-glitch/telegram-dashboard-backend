"""USDT 결제 API — API 키 발급받기 전용.

Trust boundary:
  /plans           — intentionally public (read-only plan info)
  /request-key     — intentionally public (user hasn't paid yet), rate-limited
  /status/{ref}    — intentionally public, returns masked API key only

Payment verification is done server-side by usdt_watcher.py (scheduled task)
which queries Trongrid directly — client-supplied tx data is NOT trusted here.

Plan definitions sourced from canonical app.core.plans.
"""

import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, status

from app.core.logging import get_logger
from app.core.plans import (
    PLAN_CATALOG,
    get_plan,
    get_plan_price_usdt,
    is_deprecated_plan,
)
from app.database import async_session_maker
from app.models.tenant import Tenant, PaymentRecord
from app.models.api_key import APIKey
from app.services.usage_tracker import apply_plan_limits

router = APIRouter(prefix="/api/payment", tags=["payment"])
logger = get_logger(__name__)

USDT_WALLET = "TFyAKKLYH96T1NmL92Mr7vpK87EUNnkCSc"

# In-memory rate limit: {(phone_or_ip, endpoint): timestamp}
_request_timestamps: dict[tuple[str, str], float] = {}


def _check_rate_limit(key: tuple[str, str], min_interval: float = 10.0) -> bool:
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
    """요금제 목록 + 내 지갑 주소 반환 (public).

    Returns canonical plans from PLAN_CATALOG.
    Deprecated plans (basic, enterprise) are excluded.
    """
    plans = []
    for pid, pdef in PLAN_CATALOG.items():
        entry = {
            "id": pid,
            "name": pdef["name"],
            "description": pdef["description"],
            "features": pdef["features"],
        }
        for interval, price in pdef["prices_usdt"].items():
            entry["usdt_amount"] = price
            entry["billing"] = interval
        plans.append(entry)

    return {
        "wallet_address": USDT_WALLET,
        "network": "TRC20",
        "plans": plans,
    }


@router.post("/request-key")
async def request_api_key(plan: str, phone: str = "", request: Request = None):
    """API 키 발급 요청 → USDT 송금 정보 + payment_ref 반환 (public, rate-limited).

    Validates plan against canonical PLAN_CATALOG.
    Rejects deprecated plans (basic, enterprise).
    """
    if is_deprecated_plan(plan):
        raise HTTPException(
            status_code=400,
            detail="해당 요금제는 더 이상 제공되지 않습니다. Pro ($100/월) 또는 Team ($199/분기)을 선택해주세요.",
        )

    plan_def = get_plan(plan)
    if plan_def is None:
        raise HTTPException(status_code=400, detail="유효하지 않은 요금제입니다.")

    rate_key = phone if phone else (request.client.host if request else "unknown")
    if not _check_rate_limit(("request-key", rate_key), min_interval=10.0):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="너무 많은 요청입니다. 10초 후 다시 시도해주세요.",
        )

    price = get_plan_price_usdt(plan, "monthly") or get_plan_price_usdt(plan, "quarterly") or 0
    payment_ref = f"TM-{secrets.token_hex(4).upper()}"

    async with async_session_maker() as db:
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
        "amount_usdt": price,
        "plan": plan,
        "plan_name": plan_def["name"],
        "instructions": (
            f"1. 위 {USDT_WALLET} 주소로 **{price} USDT(TRC20)**를 보내주세요.\n"
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

        result = await db.execute(select(Tenant).where(Tenant.payment_ref == payment_ref))
        tenant = result.scalar_one_or_none()

        if tenant is None or tenant.subscription_status != "active":
            return {"status": "pending", "message": "입금을 기다리는 중입니다..."}

        payment_result = await db.execute(
            select(PaymentRecord)
            .where(PaymentRecord.tenant_id == tenant.id)
            .order_by(PaymentRecord.created_at.desc())
        )
        payment = payment_result.scalars().first()

        api_key = None
        if payment is not None and payment.api_key_id is not None:
            api_key = await db.get(APIKey, payment.api_key_id)
        masked_key = (
            api_key.key[:8] + "..." + api_key.key[-4:]
            if api_key and len(api_key.key) > 12
            else "발급 완료"
        )
        return {
            "status": "completed",
            "api_key_masked": masked_key,
            "plan": tenant.plan,
            "tx_id": payment.tx_id if payment is not None else None,
        }
