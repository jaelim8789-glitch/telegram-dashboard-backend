from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_identity, Identity
from app.core.logging import get_logger
from app.database import get_db
from app.models.referral import ReferralCode, ReferralCommission
from app.models.tenant import Tenant

router = APIRouter(prefix="/api/referral", tags=["referral"])
logger = get_logger(__name__)


@router.get("/code")
async def get_my_referral_code(
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    tenant_id = identity.tenant_id or (await _resolve_tenant_id_by_identity(db, identity))
    if not tenant_id:
        raise HTTPException(status_code=404, detail="테넌트를 찾을 수 없습니다.")

    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="테넌트를 찾을 수 없습니다.")

    code = (await db.execute(select(ReferralCode).where(ReferralCode.owner_id == tenant_id))).scalar_one_or_none()
    if code is None:
        code = ReferralCode(code=tenant.referral_code, owner_id=tenant_id, is_active=True)
        db.add(code)
        await db.commit()
        await db.refresh(code)

    return {
        "code": code.code,
        "created_at": code.created_at.isoformat() if code.created_at else None,
        "is_active": code.is_active,
        "uses": tenant.referral_code_uses or 0,
    }


@router.post("/code")
async def regenerate_referral_code(
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    tenant_id = identity.tenant_id or (await _resolve_tenant_id_by_identity(db, identity))
    if not tenant_id:
        raise HTTPException(status_code=404, detail="테넌트를 찾을 수 없습니다.")

    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="테넌트를 찾을 수 없습니다.")

    import secrets
    new_code = secrets.token_urlsafe(8).upper()
    while (await db.execute(select(ReferralCode).where(ReferralCode.code == new_code))).scalar_one_or_none():
        new_code = secrets.token_urlsafe(8).upper()

    tenant.referral_code = new_code
    code = (await db.execute(select(ReferralCode).where(ReferralCode.owner_id == tenant_id))).scalar_one_or_none()
    if code:
        code.code = new_code
        code.created_at = datetime.now()
    else:
        code = ReferralCode(code=new_code, owner_id=tenant_id, is_active=True)
        db.add(code)
    await db.commit()
    await db.refresh(code)

    return {
        "code": code.code,
        "created_at": code.created_at.isoformat() if code.created_at else None,
        "is_active": code.is_active,
        "uses": tenant.referral_code_uses or 0,
    }


@router.get("/commissions")
async def get_my_commissions(
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    tenant_id = identity.tenant_id or (await _resolve_tenant_id_by_identity(db, identity))
    if not tenant_id:
        raise HTTPException(status_code=404, detail="테넌트를 찾을 수 없습니다.")

    result = await db.execute(
        select(ReferralCommission)
        .where(ReferralCommission.referrer_id == tenant_id)
        .order_by(ReferralCommission.created_at.desc())
    )
    commissions = result.scalars().all()

    total_pending = sum(c.amount_cents for c in commissions if c.status == "pending")
    total_paid = sum(c.amount_cents for c in commissions if c.status == "paid")

    return {
        "total_pending_cents": total_pending,
        "total_paid_cents": total_paid,
        "items": [
            {
                "id": c.id,
                "referred_id": c.referred_id,
                "amount_cents": c.amount_cents,
                "rate": c.rate,
                "status": c.status,
                "payment_tx_id": c.payment_tx_id,
                "paid_at": c.paid_at.isoformat() if c.paid_at else None,
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c in commissions
        ],
    }


@router.get("/stats")
async def get_referral_stats(
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    tenant_id = identity.tenant_id or (await _resolve_tenant_id_by_identity(db, identity))
    if not tenant_id:
        raise HTTPException(status_code=404, detail="테넌트를 찾을 수 없습니다.")

    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="테넌트를 찾을 수 없습니다.")

    referred_count = (await db.execute(
        select(Tenant).where(Tenant.referred_by == tenant_id)
    )).scalars().all()

    return {
        "referral_code": tenant.referral_code,
        "total_referred": len(referred_count),
        "total_earnings_cents": tenant.referral_earnings or 0,
        "uses": tenant.referral_code_uses or 0,
    }


async def _resolve_tenant_id_by_identity(db: AsyncSession, identity: Identity) -> str | None:
    if identity.kind == "user" and identity.user is not None:
        from sqlalchemy import select
        result = await db.execute(select(Tenant.id).where(Tenant.phone == identity.user.phone))
        return result.scalar_one_or_none()
    if identity.tenant_id:
        return identity.tenant_id
    return None
