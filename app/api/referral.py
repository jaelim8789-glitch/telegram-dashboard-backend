import io
import json
import random
import string

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Identity, get_current_identity, require_admin
from app.config import settings
from app.core.logging import get_logger
from app.core.rate_limiter import check_rate_limit, get_client_ip
from app.database import get_db
from app.models.referral import ReferralAuditLog, ReferralCode, ReferralCommission, ReferralConfig, ReferralPayout
from app.models.tenant import Tenant
from app.schemas.referral import (
    AdminCodeStatsItem,
    AdminCodeStatsResponse,
    AdminPendingCommissionItem,
    AdminPendingCommissionResponse,
    AdminSettingItem,
    AdminSettingsResponse,
    ChangeCodeRequest,
    CommissionItem,
    DailyStatsItem,
    DistributorListItem,
    DistributorListResponse,
    DistributorStatusResponse,
    GenerateReferralCodeResponse,
    LeaderboardEntry,
    LeaderboardResponse,
    MyCommissionsResponse,
    PayoutRecord,
    ProcessPayoutResponse,
    ReferralDashboardResponse,
    ReferralReferredUser,
    ReferralStatsResponse,
    RegisterDistributorResponse,
    RejectPayoutRequest,
    SetChatIdRequest,
    SetDistributorRateRequest,
    SettlementAuditItem,
    SettlementAuditResponse,
    SetWalletRequest,
    SuspendDistributorRequest,
    UpdateSettingsRequest,
)
from app.services.referral import (
    approve_payout,
    cancel_commission,
    generate_commissions_csv,
    generate_stats_csv,
    get_admin_code_stats,
    get_leaderboard,
    get_my_commissions,
    get_pending_payouts,
    get_referrer_tier,
    get_stats,
    process_payouts,
    set_config,
    set_wallet_address,
)

router = APIRouter(prefix="/api/referral", tags=["referral"])
public_router = APIRouter(prefix="/api/referral", tags=["referral-public"])
logger = get_logger(__name__)

MAX_GENERATION_RETRIES = 20


def _generate_code() -> str:
    prefix = random.choice(string.ascii_uppercase + string.digits)
    nums = "".join(random.choices(string.digits, k=4))
    suffix = random.choice(["별", "빛", "달", "봄", "여", "온", "연", "하", "누", "라"])
    return f"{prefix}{nums}{suffix}"


async def _get_tenant(db: AsyncSession, identity: Identity) -> Tenant:
    if not identity.tenant_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="테넌트 정보가 없습니다.")
    tenant = await db.get(Tenant, identity.tenant_id)
    if not tenant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="테넌트를 찾을 수 없습니다.")
    return tenant


async def _get_or_create_referral_code(db: AsyncSession, tenant_id: str) -> ReferralCode:
    result = await db.execute(
        select(ReferralCode).where(ReferralCode.owner_id == tenant_id)
    )
    existing = result.scalar_one_or_none()
    if existing:
        return existing

    for attempt in range(MAX_GENERATION_RETRIES):
        code = _generate_code()
        existing_code = await db.execute(
            select(ReferralCode).where(ReferralCode.code == code)
        )
        if existing_code.scalar_one_or_none() is None:
            ref_code = ReferralCode(code=code, owner_id=tenant_id)
            db.add(ref_code)
            await db.commit()
            await db.refresh(ref_code)
            return ref_code

    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="추천인 코드 생성에 실패했습니다. 잠시 후 다시 시도해주세요.",
    )


@router.post("/generate", response_model=GenerateReferralCodeResponse)
async def generate_referral_code(
    request: Request,
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    client_ip = get_client_ip(request)
    if not check_rate_limit(client_ip, "referral_generate", max_attempts=5, window_seconds=60):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="너무 많은 요청입니다. 잠시 후 다시 시도해주세요.")
    tenant = await _get_tenant(db, identity)
    ref_code = await _get_or_create_referral_code(db, tenant.id)
    return GenerateReferralCodeResponse(code=ref_code.code, referral_code_id=ref_code.id)


@router.get("/code", response_model=GenerateReferralCodeResponse)
@router.get("/my-code", response_model=GenerateReferralCodeResponse)
async def get_my_referral_code(
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    tenant = await _get_tenant(db, identity)
    result = await db.execute(
        select(ReferralCode).where(ReferralCode.owner_id == tenant.id)
    )
    ref_code = result.scalar_one_or_none()
    if not ref_code:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="추천인 코드가 없습니다. 먼저 코드를 생성해주세요.",
        )
    return GenerateReferralCodeResponse(code=ref_code.code, referral_code_id=ref_code.id)


@router.get("/my-link")
async def get_my_referral_link(
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    tenant = await _get_tenant(db, identity)
    result = await db.execute(
        select(ReferralCode).where(ReferralCode.owner_id == tenant.id)
    )
    ref_code = result.scalar_one_or_none()
    if not ref_code:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="추천인 코드가 없습니다. 먼저 코드를 생성해주세요.",
        )
    link = f"https://t.me/{settings.telegram_bot_username}?start=ref_{ref_code.code}"
    return {"link": link, "code": ref_code.code}


@router.get("/distributor-status")
async def check_distributor_status(
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    tenant = await _get_tenant(db, identity)
    return DistributorStatusResponse(is_distributor=tenant.is_distributor)


@router.post("/register-distributor", response_model=RegisterDistributorResponse)
async def register_as_distributor(
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    tenant = await _get_tenant(db, identity)
    if tenant.is_distributor:
        return RegisterDistributorResponse(
            success=True,
            message="이미 총판으로 등록되어 있습니다.",
            is_distributor=True,
        )

    tenant.is_distributor = True
    await _get_or_create_referral_code(db, tenant.id)
    await db.commit()
    await db.refresh(tenant)
    return RegisterDistributorResponse(
        success=True,
        message="총판 등록이 완료되었습니다.",
        is_distributor=True,
    )


@router.get("/commissions", response_model=MyCommissionsResponse)
@router.get("/my-commissions", response_model=MyCommissionsResponse)
async def get_my_commissions_endpoint(
    page: int = 1,
    page_size: int = 20,
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    tenant = await _get_tenant(db, identity)
    items, total_count = await get_my_commissions(db, tenant.id, page=page, page_size=page_size)
    return MyCommissionsResponse(items=[CommissionItem(**i) for i in items], total_count=total_count)


@router.post("/set-wallet")
async def set_my_wallet_address(
    payload: SetWalletRequest,
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    tenant = await _get_tenant(db, identity)
    await set_wallet_address(db, tenant.id, payload.wallet_address)
    return {"success": True, "message": "지갑 주소가 저장되었습니다."}


@router.get("/dashboard", response_model=ReferralDashboardResponse)
async def get_referral_dashboard(
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    tenant = await _get_tenant(db, identity)

    ref_code_result = await db.execute(
        select(ReferralCode).where(ReferralCode.owner_id == tenant.id)
    )
    ref_code = ref_code_result.scalar_one_or_none()

    referred_result = await db.execute(
        select(Tenant).where(Tenant.referred_by == tenant.id)
    )
    referred_tenants = list(referred_result.scalars().all())

    referred_users = []
    for rt in referred_tenants:
        has_paid = rt.subscription_status == "active" and rt.plan != "free"
        referred_users.append(ReferralReferredUser(
            tenant_id=rt.id,
            phone=rt.phone,
            plan=rt.plan,
            has_paid=has_paid,
            joined_at=rt.created_at,
        ))

    pending_sum = await db.execute(
        select(func.coalesce(func.sum(ReferralCommission.commission_amount), 0))
        .where(
            ReferralCommission.referrer_id == tenant.id,
            ReferralCommission.status == "pending",
        )
    )
    pending_total = pending_sum.scalar_one_or_none() or 0

    paid_sum = await db.execute(
        select(func.coalesce(func.sum(ReferralCommission.commission_amount), 0))
        .where(
            ReferralCommission.referrer_id == tenant.id,
            ReferralCommission.status == "paid",
        )
    )
    paid_total = paid_sum.scalar_one_or_none() or 0

    rate, tier_label = await get_referrer_tier(db, tenant.id)

    return ReferralDashboardResponse(
        my_code=ref_code.code if ref_code else None,
        referral_code_id=ref_code.id if ref_code else None,
        referred_users=referred_users,
        pending_commission_total=pending_total,
        paid_commission_total=paid_total,
    )


@router.get("/admin/pending", response_model=AdminPendingCommissionResponse)
async def get_admin_pending_commissions(
    db: AsyncSession = Depends(get_db),
    _admin: None = Depends(require_admin),
):
    result = await db.execute(
        select(ReferralCommission).where(ReferralCommission.status == "pending")
        .order_by(ReferralCommission.created_at.desc())
    )
    commissions = list(result.scalars().all())

    items = []
    for c in commissions:
        referrer = await db.get(Tenant, c.referrer_id)
        referred_user = await db.get(Tenant, c.referred_user_id)
        items.append(AdminPendingCommissionItem(
            id=c.id,
            referrer_id=c.referrer_id,
            referrer_phone=referrer.phone if referrer else "unknown",
            referred_user_phone=referred_user.phone if referred_user else "unknown",
            source_type=c.source_type,
            amount=c.amount,
            commission_rate=c.commission_rate,
            commission_amount=c.commission_amount,
            created_at=c.created_at,
        ))

    return AdminPendingCommissionResponse(items=items, total_count=len(items))


@router.post("/admin/{commission_id}/mark-paid")
async def mark_commission_paid(
    commission_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
    _admin: None = Depends(require_admin),
):
    commission = await db.get(ReferralCommission, commission_id)
    if not commission:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="해당 커미션을 찾을 수 없습니다.",
        )
    if commission.status == "paid":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="이미 지급 완료된 커미션입니다.",
        )
    commission.status = "paid"
    await db.commit()

    from app.services.referral import log_audit
    await log_audit(db, "commission.mark_paid", actor_id=identity.tenant_id, target_id=commission_id, details=f"Commission {commission_id} marked paid manually")
    return {"success": True, "message": "커미션이 지급 완료 처리되었습니다."}


@router.post("/admin/process-payouts", response_model=ProcessPayoutResponse)
async def admin_process_payouts(
    db: AsyncSession = Depends(get_db),
    _admin: None = Depends(require_admin),
):
    payouts_created, total_amount = await process_payouts(db)
    return ProcessPayoutResponse(
        success=True,
        payouts_created=payouts_created,
        total_amount=total_amount,
        message=f"{payouts_created}명의 추천인에 대한 지급대상이 생성되었습니다. 승인 후 실제 지급됩니다." if payouts_created else "지급할 커미션이 없습니다.",
    )


@router.get("/admin/payouts/pending")
async def get_admin_pending_payouts(
    db: AsyncSession = Depends(get_db),
    _admin: None = Depends(require_admin),
):
    payouts = await get_pending_payouts(db)
    items = []
    for p in payouts:
        referrer = await db.get(Tenant, p.referrer_id)
        items.append(PayoutRecord(
            id=p.id,
            referrer_id=p.referrer_id,
            referrer_phone=referrer.phone if referrer else "unknown",
            amount=p.amount,
            status=p.status,
            paid_at=p.paid_at,
            created_at=p.created_at,
        ))
    return {"items": items, "total_count": len(items)}


@router.post("/admin/payouts/{payout_id}/approve")
async def admin_approve_payout(
    payout_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
    _admin: None = Depends(require_admin),
):
    success = await approve_payout(db, payout_id, actor_id=identity.tenant_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="해당 지급대상을 찾을 수 없거나 이미 처리되었습니다.",
        )
    return {"success": True, "message": "지급이 승인되었습니다. 관련 커미션이 지급 완료 처리되었습니다."}


@router.get("/admin/payouts")
async def get_admin_payouts(
    db: AsyncSession = Depends(get_db),
    _admin: None = Depends(require_admin),
):
    result = await db.execute(
        select(ReferralPayout).order_by(ReferralPayout.created_at.desc()).limit(50)
    )
    payouts = list(result.scalars().all())

    items = []
    for p in payouts:
        referrer = await db.get(Tenant, p.referrer_id)
        items.append(PayoutRecord(
            id=p.id,
            referrer_id=p.referrer_id,
            referrer_phone=referrer.phone if referrer else "unknown",
            amount=p.amount,
            status=p.status,
            paid_at=p.paid_at,
            created_at=p.created_at,
        ))
    return {"items": items, "total_count": len(items)}


@router.get("/stats", response_model=ReferralStatsResponse)
async def get_referral_stats(
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    data = await get_stats(db)
    return ReferralStatsResponse(
        total_referrers=data["total_referrers"],
        total_referred=data["total_referred"],
        total_commissions_pending=data["total_commissions_pending"],
        total_commissions_paid=data["total_commissions_paid"],
        total_commission_amount_pending=data["total_commission_amount_pending"],
        total_commission_amount_paid=data["total_commission_amount_paid"],
        daily=[DailyStatsItem(**d) for d in data["daily"]],
    )


@router.post("/set-chat-id")
async def set_telegram_chat_id(
    payload: SetChatIdRequest,
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    tenant = await _get_tenant(db, identity)
    tenant.telegram_chat_id = payload.chat_id
    await db.commit()
    return {"success": True, "message": "텔레그램 알림이 설정되었습니다."}


@router.post("/admin/commissions/{commission_id}/cancel")
async def admin_cancel_commission(
    commission_id: str,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
    _admin: None = Depends(require_admin),
):
    success = await cancel_commission(db, commission_id, actor_id=identity.tenant_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="해당 커미션을 찾을 수 없거나 이미 취소되었습니다.",
        )
    return {"success": True, "message": "커미션이 취소되었습니다."}


@router.post("/change-code")
async def change_referral_code(
    request: Request,
    payload: ChangeCodeRequest,
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    client_ip = get_client_ip(request)
    if not check_rate_limit(client_ip, "referral_change_code", max_attempts=3, window_seconds=300):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="너무 많은 요청입니다. 잠시 후 다시 시도해주세요.")

    tenant = await _get_tenant(db, identity)
    result = await db.execute(
        select(ReferralCode).where(ReferralCode.owner_id == tenant.id)
    )
    ref_code = result.scalar_one_or_none()
    if not ref_code:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="추천인 코드가 없습니다.")

    existing = await db.execute(
        select(ReferralCode).where(
            ReferralCode.code == payload.new_code,
            ReferralCode.owner_id != tenant.id,
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="이미 사용 중인 코드입니다.")

    old_code = ref_code.code
    ref_code.code = payload.new_code
    await db.commit()

    from app.services.referral import log_audit
    await log_audit(db, "code.change", actor_id=tenant.id, target_id=ref_code.id, details=f"Code changed: {old_code} -> {payload.new_code}")

    return {"success": True, "code": payload.new_code, "message": "코드가 변경되었습니다."}


@router.get("/my-qr")
async def get_referral_qr(
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    import qrcode

    tenant = await _get_tenant(db, identity)
    result = await db.execute(
        select(ReferralCode).where(ReferralCode.owner_id == tenant.id)
    )
    ref_code = result.scalar_one_or_none()
    if not ref_code:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="추천인 코드가 없습니다.")

    link = f"https://t.me/{settings.telegram_bot_username}?start=ref_{ref_code.code}"
    img = qrcode.make(link)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png", headers={"Content-Disposition": "inline; filename=referral_qr.png"})


@router.get("/leaderboard", response_model=LeaderboardResponse)
async def get_referral_leaderboard(
    db: AsyncSession = Depends(get_db),
):
    entries = await get_leaderboard(db)
    return LeaderboardResponse(items=[LeaderboardEntry(**e) for e in entries])


@router.get("/admin/settings", response_model=AdminSettingsResponse)
async def get_admin_settings(
    db: AsyncSession = Depends(get_db),
    _admin: None = Depends(require_admin),
):
    from app.services.referral import get_config

    tiers_raw = await get_config(db, "tiers")
    min_payout = await get_config(db, "min_payout", "100")
    settings_list = []
    if tiers_raw:
        settings_list.append(AdminSettingItem(key="tiers", value=tiers_raw))
    settings_list.append(AdminSettingItem(key="min_payout", value=min_payout))
    return AdminSettingsResponse(settings=settings_list)


@router.put("/admin/settings")
async def update_admin_settings(
    payload: UpdateSettingsRequest,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
    _admin: None = Depends(require_admin),
):
    for s in payload.settings:
        await set_config(db, s.key, s.value)

    from app.services.referral import log_audit
    await log_audit(db, "settings.update", actor_id=identity.tenant_id, details=f"Settings updated: {[s.key for s in payload.settings]}")

    return {"success": True, "message": "설정이 저장되었습니다."}


@router.get("/admin/distributors", response_model=DistributorListResponse)
async def list_distributors(
    db: AsyncSession = Depends(get_db),
    _admin: None = Depends(require_admin),
):
    from app.services.referral import get_config

    codes_result = await db.execute(
        select(ReferralCode.owner_id).distinct()
    )
    owner_ids = [row[0] for row in codes_result.all()]

    items = []
    for owner_id in owner_ids:
        tenant = await db.get(Tenant, owner_id)
        if not tenant:
            continue

        ref_code_result = await db.execute(
            select(ReferralCode).where(ReferralCode.owner_id == tenant.id)
        )
        ref_code = ref_code_result.scalar_one_or_none()

        count_result = await db.execute(
            select(func.count())
            .select_from(ReferralCommission)
            .where(
                ReferralCommission.referrer_id == tenant.id,
                ReferralCommission.status.in_(["pending", "paid"]),
            )
        )
        referral_count = count_result.scalar_one() or 0

        amount_result = await db.execute(
            select(func.coalesce(func.sum(ReferralCommission.amount), 0))
            .where(
                ReferralCommission.referrer_id == tenant.id,
                ReferralCommission.status.in_(["pending", "paid"]),
            )
        )
        total_revenue = amount_result.scalar_one() or 0

        commission_result = await db.execute(
            select(func.coalesce(func.sum(ReferralCommission.commission_amount), 0))
            .where(
                ReferralCommission.referrer_id == tenant.id,
                ReferralCommission.status.in_(["pending", "paid"]),
            )
        )
        total_commission = commission_result.scalar_one() or 0

        payout_result = await db.execute(
            select(func.coalesce(func.sum(ReferralPayout.amount), 0))
            .where(
                ReferralPayout.referrer_id == tenant.id,
                ReferralPayout.status == "completed",
            )
        )
        total_payout = payout_result.scalar_one() or 0

        rate_raw = await get_config(db, f"commission_rate:{tenant.id}")
        commission_rate_override = float(rate_raw) if rate_raw else None

        status_raw = await get_config(db, f"distributor_status:{tenant.id}")
        status = "suspended" if status_raw == "suspended" else "active"

        items.append(DistributorListItem(
            tenant_id=tenant.id,
            phone=tenant.phone,
            plan=tenant.plan,
            referral_code=ref_code.code if ref_code else "",
            referral_count=referral_count,
            total_revenue=total_revenue,
            total_commission=total_commission,
            total_payout=total_payout,
            commission_rate_override=commission_rate_override,
            status=status,
            created_at=tenant.created_at,
        ))

    items.sort(key=lambda x: x.referral_count, reverse=True)
    return DistributorListResponse(items=items, total_count=len(items))


@router.post("/admin/distributors/{tenant_id}/rate")
async def set_distributor_rate(
    tenant_id: str,
    payload: SetDistributorRateRequest,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
    _admin: None = Depends(require_admin),
):
    from app.services.referral import log_audit

    tenant = await db.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    key = f"commission_rate:{tenant_id}"
    if payload.rate == 0.0:
        result = await db.execute(select(ReferralConfig).where(ReferralConfig.key == key))
        existing = result.scalar_one_or_none()
        if existing:
            await db.delete(existing)
            await db.commit()
    else:
        result = await db.execute(select(ReferralConfig).where(ReferralConfig.key == key))
        existing = result.scalar_one_or_none()
        if existing:
            existing.value = str(payload.rate)
        else:
            db.add(ReferralConfig(key=key, value=str(payload.rate)))
        await db.commit()

    await log_audit(db, "rate.override", actor_id=identity.tenant_id, target_id=tenant_id, details=f"Commission rate set to {payload.rate}")
    return {"success": True, "rate": payload.rate}


@router.post("/admin/distributors/{tenant_id}/suspend")
async def suspend_distributor(
    tenant_id: str,
    payload: SuspendDistributorRequest,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
    _admin: None = Depends(require_admin),
):
    from app.services.referral import log_audit

    tenant = await db.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    key = f"distributor_status:{tenant_id}"
    if payload.suspended:
        result = await db.execute(select(ReferralConfig).where(ReferralConfig.key == key))
        existing = result.scalar_one_or_none()
        if existing:
            existing.value = "suspended"
        else:
            db.add(ReferralConfig(key=key, value="suspended"))
        await db.commit()
        await log_audit(db, "distributor.suspend", actor_id=identity.tenant_id, target_id=tenant_id, details=f"Distributor suspended. Reason: {payload.reason}")
        return {"success": True, "status": "suspended", "reason": payload.reason}
    else:
        result = await db.execute(select(ReferralConfig).where(ReferralConfig.key == key))
        existing = result.scalar_one_or_none()
        if existing:
            await db.delete(existing)
            await db.commit()
        await log_audit(db, "distributor.unsuspend", actor_id=identity.tenant_id, target_id=tenant_id, details=f"Distributor unsuspended. Reason: {payload.reason}")
        return {"success": True, "status": "active", "reason": payload.reason}


@router.post("/admin/payouts/{payout_id}/reject")
async def reject_payout(
    payout_id: str,
    payload: RejectPayoutRequest,
    db: AsyncSession = Depends(get_db),
    identity: Identity = Depends(get_current_identity),
    _admin: None = Depends(require_admin),
):
    from app.services.referral import log_audit

    payout = await db.get(ReferralPayout, payout_id)
    if not payout:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payout not found")
    if payout.status != "pending":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Payout is not in pending status")

    payout.status = "rejected"
    await db.commit()
    await log_audit(db, "payout.reject", actor_id=identity.tenant_id, target_id=payout_id, details=f"Payout rejected. Reason: {payload.reason}")
    return {"success": True, "message": "Payout has been rejected."}


@router.get("/admin/audit/settlements", response_model=SettlementAuditResponse)
async def get_settlement_audit_logs(
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _admin: None = Depends(require_admin),
):
    result = await db.execute(
        select(ReferralAuditLog)
        .where(
            ReferralAuditLog.action.contains("payout"),
        )
        .order_by(ReferralAuditLog.created_at.desc())
        .limit(limit)
    )
    payout_logs = list(result.scalars().all())

    result2 = await db.execute(
        select(ReferralAuditLog)
        .where(
            ReferralAuditLog.action.contains("commission"),
            ~ReferralAuditLog.action.contains("payout"),
        )
        .order_by(ReferralAuditLog.created_at.desc())
        .limit(limit)
    )
    commission_logs = list(result2.scalars().all())

    result3 = await db.execute(
        select(ReferralAuditLog)
        .where(
            ReferralAuditLog.action.contains("rate"),
            ~ReferralAuditLog.action.contains("payout"),
            ~ReferralAuditLog.action.contains("commission"),
        )
        .order_by(ReferralAuditLog.created_at.desc())
        .limit(limit)
    )
    rate_logs = list(result3.scalars().all())

    combined = sorted(
        payout_logs + commission_logs + rate_logs,
        key=lambda x: x.created_at,
        reverse=True,
    )[:limit]

    items = [SettlementAuditItem(
        id=entry.id,
        action=entry.action,
        actor_id=entry.actor_id,
        target_id=entry.target_id,
        details=entry.details,
        created_at=entry.created_at,
    ) for entry in combined]
    return SettlementAuditResponse(items=items)


@router.get("/admin/codes", response_model=AdminCodeStatsResponse)
async def get_admin_codes(
    db: AsyncSession = Depends(get_db),
    _admin: None = Depends(require_admin),
):
    items = await get_admin_code_stats(db)
    return AdminCodeStatsResponse(items=[AdminCodeStatsItem(**i) for i in items])
