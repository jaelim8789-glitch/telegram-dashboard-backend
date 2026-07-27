import io
import json
import random
import string

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Identity, get_current_identity, require_admin
from app.config import settings
from app.core.logging import get_logger
from app.core.rate_limiter import check_rate_limit, get_client_ip
from app.database import get_db
from app.models.referral import ReferralCode, ReferralCommission, ReferralConfig, ReferralPayout
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
    GenerateReferralCodeResponse,
    LeaderboardEntry,
    LeaderboardResponse,
    MyCommissionsResponse,
    PayoutRecord,
    ProcessPayoutResponse,
    ReferralDashboardResponse,
    ReferralReferredUser,
    ReferralStatsResponse,
    SetChatIdRequest,
    SetWalletRequest,
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

router = APIRouter(prefix="/api/referrals", tags=["referrals"])
public_router = APIRouter(prefix="/api/referrals", tags=["referrals-public"])
logger = get_logger(__name__)

MAX_GENERATION_RETRIES = 20


def _generate_code() -> str:
    prefix = random.choice(string.ascii_uppercase + string.digits)
    nums = "".join(random.choices(string.digits, k=4))
    suffix = random.choice(["蹂?, "鍮?, "??, "遊?, "??, "??, "??, "??, "??, "??])
    return f"{prefix}{nums}{suffix}"


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
        detail="異붿쿇??肄붾뱶 ?앹꽦???ㅽ뙣?덉뒿?덈떎. ?좎떆 ???ㅼ떆 ?쒕룄?댁＜?몄슂.",
    )


@router.post("/generate", response_model=GenerateReferralCodeResponse)
async def generate_referral_code(
    request: Request,
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    client_ip = get_client_ip(request)
    if not check_rate_limit(client_ip, "referral_generate", max_attempts=5, window_seconds=60):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="?덈Т 留롮? ?붿껌?낅땲?? ?좎떆 ???ㅼ떆 ?쒕룄?댁＜?몄슂.")

    ref_code = await _get_or_create_referral_code(db, identity.tenant_id)
    return GenerateReferralCodeResponse(code=ref_code.code, referral_code_id=ref_code.id)


@router.get("/my-code", response_model=GenerateReferralCodeResponse)
async def get_my_referral_code(
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(ReferralCode).where(ReferralCode.owner_id == identity.tenant_id)
    )
    ref_code = result.scalar_one_or_none()
    if not ref_code:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="異붿쿇??肄붾뱶媛 ?놁뒿?덈떎. 癒쇱? 肄붾뱶瑜??앹꽦?댁＜?몄슂.",
        )
    return GenerateReferralCodeResponse(code=ref_code.code, referral_code_id=ref_code.id)


@router.get("/my-link")
async def get_my_referral_link(
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(ReferralCode).where(ReferralCode.owner_id == identity.tenant_id)
    )
    ref_code = result.scalar_one_or_none()
    if not ref_code:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="異붿쿇??肄붾뱶媛 ?놁뒿?덈떎. 癒쇱? 肄붾뱶瑜??앹꽦?댁＜?몄슂.",
        )
    link = f"https://t.me/{settings.telegram_bot_username}?start=ref_{ref_code.code}"
    return {"link": link, "code": ref_code.code}


@router.get("/my-commissions", response_model=MyCommissionsResponse)
async def get_my_commissions_endpoint(
    page: int = 1,
    page_size: int = 20,
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    items, total_count = await get_my_commissions(db, identity.tenant_id, page=page, page_size=page_size)
    return MyCommissionsResponse(items=[CommissionItem(**i) for i in items], total_count=total_count)


@router.post("/set-wallet")
async def set_my_wallet_address(
    payload: SetWalletRequest,
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    await set_wallet_address(db, identity.tenant_id, payload.wallet_address)
    return {"success": True, "message": "吏媛?二쇱냼媛 ??λ릺?덉뒿?덈떎."}


@router.get("/dashboard", response_model=ReferralDashboardResponse)
async def get_referral_dashboard(
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    ref_code_result = await db.execute(
        select(ReferralCode).where(ReferralCode.owner_id == identity.tenant_id)
    )
    ref_code = ref_code_result.scalar_one_or_none()

    referred_result = await db.execute(
        select(Tenant).where(identity.referred_by == (ref_code.id if ref_code else None))
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
            ReferralCommission.referrer_id == identity.tenant_id,
            ReferralCommission.status == "pending",
        )
    )
    pending_total = pending_sum.scalar_one_or_none() or 0

    paid_sum = await db.execute(
        select(func.coalesce(func.sum(ReferralCommission.commission_amount), 0))
        .where(
            ReferralCommission.referrer_id == identity.tenant_id,
            ReferralCommission.status == "paid",
        )
    )
    paid_total = paid_sum.scalar_one_or_none() or 0

    rate, tier_label = await get_referrer_tier(db, identity.tenant_id)

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
            detail="?대떦 而ㅻ??섏쓣 李얠쓣 ???놁뒿?덈떎.",
        )
    if commission.status == "paid":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="?대? 吏湲??꾨즺??而ㅻ??섏엯?덈떎.",
        )
    commission.status = "paid"
    await db.commit()

    from app.services.referral import log_audit
    await log_audit(db, "commission.mark_paid", actor_id=identity.tenant_id, target_id=commission_id, details=f"Commission {commission_id} marked paid manually")
    return {"success": True, "message": "而ㅻ??섏씠 吏湲??꾨즺 泥섎━?섏뿀?듬땲??"}


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
        message=f"{payouts_created}紐낆쓽 異붿쿇?몄뿉 ???吏湲됰??곸씠 ?앹꽦?섏뿀?듬땲?? ?뱀씤 ???ㅼ젣 吏湲됰맗?덈떎." if payouts_created else "吏湲됲븷 而ㅻ??섏씠 ?놁뒿?덈떎.",
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
            detail="?대떦 吏湲됰??곸쓣 李얠쓣 ???녾굅???대? 泥섎━?섏뿀?듬땲??",
        )
    return {"success": True, "message": "吏湲됱씠 ?뱀씤?섏뿀?듬땲?? 愿??而ㅻ??섏씠 吏湲??꾨즺 泥섎━?섏뿀?듬땲??"}


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
    Tenant.telegram_chat_id = payload.chat_id
    await db.commit()
    return {"success": True, "message": "?붾젅洹몃옩 ?뚮┝???ㅼ젙?섏뿀?듬땲??"}


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
            detail="?대떦 而ㅻ??섏쓣 李얠쓣 ???녾굅???대? 痍⑥냼?섏뿀?듬땲??",
        )
    return {"success": True, "message": "而ㅻ??섏씠 痍⑥냼?섏뿀?듬땲??"}


@router.get("/stats/csv")
async def get_referral_stats_csv(
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    data = await get_stats(db)
    csv_content = generate_stats_csv(data)
    return StreamingResponse(
        io.StringIO(csv_content),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=referral_stats.csv"},
    )


@router.get("/admin/commissions/csv")
async def get_admin_commissions_csv(
    db: AsyncSession = Depends(get_db),
    _admin: None = Depends(require_admin),
):
    result = await db.execute(
        select(ReferralCommission).order_by(ReferralCommission.created_at.desc())
    )
    commissions = list(result.scalars().all())
    items = []
    for c in commissions:
        referrer = await db.get(Tenant, c.referrer_id)
        referred = await db.get(Tenant, c.referred_user_id)
        items.append({
            "id": c.id,
            "referrer_id": c.referrer_id,
            "referrer_phone": referrer.phone if referrer else "",
            "referred_user_phone": referred.phone if referred else "",
            "source_type": c.source_type,
            "amount": c.amount,
            "commission_rate": c.commission_rate,
            "commission_amount": c.commission_amount,
            "status": c.status,
            "created_at": c.created_at,
        })
    csv_content = generate_commissions_csv(items)
    return StreamingResponse(
        io.StringIO(csv_content),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=referral_commissions.csv"},
    )


@router.post("/change-code")
async def change_referral_code(
    request: Request,
    payload: ChangeCodeRequest,
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    client_ip = get_client_ip(request)
    if not check_rate_limit(client_ip, "referral_change_code", max_attempts=3, window_seconds=300):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="?덈Т 留롮? ?붿껌?낅땲?? ?좎떆 ???ㅼ떆 ?쒕룄?댁＜?몄슂.")

    result = await db.execute(
        select(ReferralCode).where(ReferralCode.owner_id == identity.tenant_id)
    )
    ref_code = result.scalar_one_or_none()
    if not ref_code:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="異붿쿇??肄붾뱶媛 ?놁뒿?덈떎.")

    existing = await db.execute(
        select(ReferralCode).where(
            ReferralCode.code == payload.new_code,
            ReferralCode.owner_id != identity.tenant_id,
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="?대? ?ъ슜 以묒씤 肄붾뱶?낅땲??")

    old_code = ref_code.code
    ref_code.code = payload.new_code
    await db.commit()

    from app.services.referral import log_audit
    await log_audit(db, "code.change", actor_id=identity.tenant_id, target_id=ref_code.id, details=f"Code changed: {old_code} -> {payload.new_code}")

    return {"success": True, "code": payload.new_code, "message": "肄붾뱶媛 蹂寃쎈릺?덉뒿?덈떎."}


@router.get("/my-qr")
async def get_referral_qr(
    identity: Identity = Depends(get_current_identity),
    db: AsyncSession = Depends(get_db),
):
    import qrcode

    result = await db.execute(
        select(ReferralCode).where(ReferralCode.owner_id == identity.tenant_id)
    )
    ref_code = result.scalar_one_or_none()
    if not ref_code:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="異붿쿇??肄붾뱶媛 ?놁뒿?덈떎.")

    link = f"https://t.me/{settings.telegram_bot_username}?start=ref_{ref_code.code}"
    img = qrcode.make(link)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png", headers={"Content-Disposition": "inline; filename=referral_qr.png"})


@public_router.get("/leaderboard", response_model=LeaderboardResponse)
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

    return {"success": True, "message": "?ㅼ젙????λ릺?덉뒿?덈떎."}


@router.get("/admin/codes", response_model=AdminCodeStatsResponse)
async def get_admin_codes(
    db: AsyncSession = Depends(get_db),
    _admin: None = Depends(require_admin),
):
    items = await get_admin_code_stats(db)
    return AdminCodeStatsResponse(items=[AdminCodeStatsItem(**i) for i in items])
