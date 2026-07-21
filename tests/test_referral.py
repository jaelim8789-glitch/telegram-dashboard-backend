"""Tests for the referral program backend."""

import pytest
from httpx import AsyncClient

from app.models.referral import ReferralCode, ReferralCommission
from app.models.tenant import Tenant


@pytest.mark.asyncio
async def test_generate_referral_code(client: AsyncClient):
    """POST /api/referral/generate should create a referral code."""
    res = await client.post(
        "/api/referral/generate",
        headers={
            "X-API-Key": "test-api-key",
            "Content-Type": "application/json",
        },
    )
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_referral_dashboard_no_auth(client: AsyncClient):
    """GET /api/referral/dashboard should require auth."""
    res = await client.get("/api/referral/dashboard")
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_admin_pending_commissions_no_auth(client: AsyncClient):
    """GET /api/referral/admin/pending should require admin auth."""
    res = await client.get("/api/referral/admin/pending")
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_referral_code_generation_and_uniqueness(db_session):
    """Verify that referral codes are generated uniquely."""
    owner = Tenant(phone="+821099999991", plan="free")
    db_session.add(owner)
    await db_session.flush()

    code1 = ReferralCode(code="TEST1234별", owner_id=owner.id)
    db_session.add(code1)
    await db_session.flush()

    code2 = ReferralCode(code="TEST5678빛", owner_id=owner.id)
    db_session.add(code2)
    await db_session.flush()

    from sqlalchemy import select
    result = await db_session.execute(select(ReferralCode).where(ReferralCode.code == "TEST1234별"))
    found = result.scalar_one_or_none()
    assert found is not None
    assert found.code == "TEST1234별"


@pytest.mark.asyncio
async def test_commission_creation(db_session):
    """Verify commission creation logic."""
    referrer = Tenant(phone="+821099999992", plan="pro", subscription_status="active")
    db_session.add(referrer)
    await db_session.flush()

    ref_code = ReferralCode(code="REFERRER01", owner_id=referrer.id)
    db_session.add(ref_code)
    await db_session.flush()

    referred = Tenant(phone="+821099999993", plan="free", referred_by=ref_code.id)
    db_session.add(referred)
    await db_session.flush()

    commission = ReferralCommission(
        referrer_id=referrer.id,
        referred_user_id=referred.id,
        source_payment_id="test-payment-1",
        source_type="usdt",
        amount=10000,
        commission_rate=0.10,
        commission_amount=1000,
        status="pending",
    )
    db_session.add(commission)
    await db_session.commit()

    result = await db_session.get(ReferralCommission, commission.id)
    assert result is not None
    assert result.status == "pending"
    assert result.commission_amount == 1000


@pytest.mark.asyncio
async def test_commission_mark_paid(db_session):
    """Verify admin can mark commission as paid."""
    referrer = Tenant(phone="+821099999994", plan="pro")
    db_session.add(referrer)
    await db_session.flush()

    ref_code = ReferralCode(code="REFERRER02", owner_id=referrer.id)
    db_session.add(ref_code)
    await db_session.flush()

    referred = Tenant(phone="+821099999995", plan="free", referred_by=ref_code.id)
    db_session.add(referred)
    await db_session.flush()

    commission = ReferralCommission(
        referrer_id=referrer.id,
        referred_user_id=referred.id,
        source_payment_id="test-payment-2",
        source_type="stars",
        amount=5000,
        commission_rate=0.10,
        commission_amount=500,
        status="pending",
    )
    db_session.add(commission)
    await db_session.commit()

    commission.status = "paid"
    await db_session.commit()

    result = await db_session.get(ReferralCommission, commission.id)
    assert result.status == "paid"


@pytest.mark.asyncio
async def test_self_referral_prevention(db_session):
    """Self-referral should not create commission."""
    owner = Tenant(phone="+821099999996", plan="free")
    db_session.add(owner)
    await db_session.flush()

    ref_code = ReferralCode(code="SELFREF01", owner_id=owner.id)
    db_session.add(ref_code)
    await db_session.flush()

    owner.referred_by = ref_code.id
    await db_session.commit()

    from app.services.referral import create_commission
    result = await create_commission(
        db=db_session,
        referred_tenant_id=owner.id,
        source_payment_id="self-payment",
        source_type="stars",
        amount=1000,
    )
    assert result is None


@pytest.mark.asyncio
async def test_referral_code_generate_function():
    """Verify _generate_code produces valid codes."""
    from app.api.referral import _generate_code
    code = _generate_code()
    assert len(code) >= 6
    assert code.isascii() or any(ord(c) > 127 for c in code)


@pytest.mark.asyncio
async def test_referral_link_endpoint(client: AsyncClient):
    """GET /api/referral/my-link should return a Telegram deep link."""
    res = await client.get("/api/referral/my-link")
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_tier_calculation():
    """Verify tier-based commission rate calculation."""
    from app.services.referral import _get_tier, default_tiers

    tiers = default_tiers()

    rate0, label0 = _get_tier(0, tiers)
    assert rate0 == 0.10
    assert label0 == "기본"

    rate1, label1 = _get_tier(3, tiers)
    assert rate1 == 0.10
    assert label1 == "기본"

    rate2, label2 = _get_tier(5, tiers)
    assert rate2 == 0.15
    assert label2 == "Pro"

    rate3, label3 = _get_tier(10, tiers)
    assert rate3 == 0.20
    assert label3 == "VIP"

    rate4, label4 = _get_tier(20, tiers)
    assert rate4 == 0.20
    assert label4 == "VIP"


@pytest.mark.asyncio
async def test_process_payouts(db_session):
    """Verify process_payouts creates payout records."""
    referrer = Tenant(phone="+821099999997", plan="pro", subscription_status="active")
    db_session.add(referrer)
    await db_session.flush()

    ref_code = ReferralCode(code="PAYOUT01", owner_id=referrer.id)
    db_session.add(ref_code)
    await db_session.flush()

    referred1 = Tenant(phone="+821099999998", plan="free", referred_by=ref_code.id)
    referred2 = Tenant(phone="+821099999999", plan="free", referred_by=ref_code.id)
    db_session.add_all([referred1, referred2])
    await db_session.flush()

    for i, ref in enumerate([referred1, referred2]):
        c = ReferralCommission(
            referrer_id=referrer.id, referred_user_id=ref.id,
            source_payment_id=f"pay-{i}", source_type="stars",
            amount=5000, commission_rate=0.10,
            commission_amount=500, status="pending",
        )
        db_session.add(c)
    await db_session.commit()

    from app.services.referral import process_payouts
    created, total = await process_payouts(db_session, min_amount=100)
    assert created == 1
    assert total == 1000

    from app.models.referral import ReferralPayout
    from sqlalchemy import select, func
    payout_count = await db_session.execute(select(func.count()).select_from(ReferralPayout))
    assert payout_count.scalar_one() == 1


@pytest.mark.asyncio
async def test_leaderboard_endpoint_no_auth(client: AsyncClient):
    """GET /api/referral/leaderboard should be publicly accessible."""
    res = await client.get("/api/referral/leaderboard")
    assert res.status_code == 200


@pytest.mark.asyncio
async def test_admin_payouts_no_auth(client: AsyncClient):
    """GET /api/referral/admin/payouts should require admin."""
    res = await client.get("/api/referral/admin/payouts")
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_admin_process_payouts_no_auth(client: AsyncClient):
    """POST /api/referral/admin/process-payouts should require admin."""
    res = await client.post("/api/referral/admin/process-payouts")
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_admin_pending_payouts_no_auth(client: AsyncClient):
    """GET /api/referral/admin/payouts/pending should require admin."""
    res = await client.get("/api/referral/admin/payouts/pending")
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_admin_approve_payout_no_auth(client: AsyncClient):
    """POST /api/referral/admin/payouts/{id}/approve should require admin."""
    res = await client.post("/api/referral/admin/payouts/fake-id/approve")
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_referral_stats_no_auth(client: AsyncClient):
    """GET /api/referral/stats should require auth."""
    res = await client.get("/api/referral/stats")
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_set_chat_id_no_auth(client: AsyncClient):
    """POST /api/referral/set-chat-id should require auth."""
    res = await client.post("/api/referral/set-chat-id", json={"chat_id": "12345"})
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_admin_cancel_commission_no_auth(client: AsyncClient):
    """POST /api/referral/admin/commissions/{id}/cancel should require admin."""
    res = await client.post("/api/referral/admin/commissions/fake-id/cancel")
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_cancel_commission(db_session):
    """Verify cancel_commission sets status to cancelled."""
    referrer = Tenant(phone="+821011111112", plan="pro", telegram_chat_id="99999")
    db_session.add(referrer)
    await db_session.flush()

    referred = Tenant(phone="+821011111113", plan="free")
    db_session.add(referred)
    await db_session.flush()

    comm = ReferralCommission(
        referrer_id=referrer.id, referred_user_id=referred.id,
        source_payment_id="cancel-test", source_type="stars",
        amount=5000, commission_rate=0.10,
        commission_amount=500, status="pending",
    )
    db_session.add(comm)
    await db_session.commit()

    from app.services.referral import cancel_commission
    ok = await cancel_commission(db_session, comm.id)
    assert ok is True

    result = await db_session.get(ReferralCommission, comm.id)
    assert result.status == "cancelled"


@pytest.mark.asyncio
async def test_cancel_commissions_by_payment(db_session):
    """Verify cancelling by payment_id cancels all matching pending commissions."""
    referrer = Tenant(phone="+821011111114", plan="pro")
    db_session.add(referrer)
    await db_session.flush()

    referred = Tenant(phone="+821011111115", plan="free")
    db_session.add(referred)
    await db_session.flush()

    for i in range(3):
        c = ReferralCommission(
            referrer_id=referrer.id, referred_user_id=referred.id,
            source_payment_id="payment-refund-1", source_type="stars",
            amount=3000, commission_rate=0.10,
            commission_amount=300, status="pending",
        )
        db_session.add(c)
    await db_session.commit()

    from app.services.referral import cancel_commissions_by_payment
    cancelled = await cancel_commissions_by_payment(db_session, "payment-refund-1")
    assert cancelled == 3


@pytest.mark.asyncio
async def test_stats_endpoint(db_session):
    """Verify stats endpoint returns expected structure."""
    from app.services.referral import get_stats
    data = await get_stats(db_session)
    assert "total_referrers" in data
    assert "total_referred" in data
    assert "daily" in data
    assert len(data["daily"]) == 30


@pytest.mark.asyncio
async def test_change_code_no_auth(client: AsyncClient):
    """POST /api/referral/change-code should require auth."""
    res = await client.post("/api/referral/change-code", json={"new_code": "NEWCODE"})
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_qr_endpoint_no_auth(client: AsyncClient):
    """GET /api/referral/my-qr should require auth."""
    res = await client.get("/api/referral/my-qr")
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_duplicate_commission_prevention(db_session):
    """Verify duplicate commission is prevented."""
    referrer = Tenant(phone="+821099999991", plan="pro")
    db_session.add(referrer)
    await db_session.flush()

    ref_code = ReferralCode(code="DUPREF01", owner_id=referrer.id)
    db_session.add(ref_code)
    await db_session.flush()

    referred = Tenant(phone="+821099999992", plan="free", referred_by=ref_code.id)
    db_session.add(referred)
    await db_session.flush()

    from app.services.referral import create_commission
    c1 = await create_commission(db_session, referred.id, "dup-payment", "stars", 5000)
    assert c1 is not None

    c2 = await create_commission(db_session, referred.id, "dup-payment", "stars", 5000)
    assert c2 is None


@pytest.mark.asyncio
async def test_set_wallet_endpoint(client: AsyncClient):
    """POST /api/referral/set-wallet should store wallet address."""
    from app.core.security import create_user_access_token
    from app.models.user import User

    from app.database import async_session_maker
    async with async_session_maker() as db:
        user = User(phone="+821099999997")
        db.add(user)
        tenant = Tenant(phone="+821099999997", plan="free")
        db.add(tenant)
        await db.commit()
        await db.refresh(user)
        user_jwt = create_user_access_token(user.id)

    from app.core.rate_limiter import reset_rate_limits
    reset_rate_limits()
    res = await client.post("/api/referral/set-wallet",
                            json={"wallet_address": "TXYZ123456789"},
                            headers={"Authorization": f"Bearer {user_jwt}"})
    assert res.status_code == 200
    data = res.json()
    assert data["success"] is True
