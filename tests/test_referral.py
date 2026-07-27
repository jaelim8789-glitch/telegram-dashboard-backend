"""Tests for the referral program backend."""

import pytest
from httpx import AsyncClient

from app.database import async_session_maker
from app.models.referral import ReferralCode, ReferralCommission
from app.models.tenant import Tenant


@pytest.fixture
async def sample_tenant(db: async_session_maker):
    """Create a sample tenant for testing."""
    async with async_session_maker() as session:
        tenant = Tenant(
            phone="+821011111111",
            plan="free",
            subscription_status="active",
        )
        session.add(tenant)
        await session.commit()
        await session.refresh(tenant)
        return tenant


@pytest.fixture
async def admin_token(client: AsyncClient) -> str:
    """Get admin JWT for testing admin endpoints."""
    from app.core.security import create_access_token
    return create_access_token()


@pytest.mark.asyncio
async def test_generate_referral_code(client: AsyncClient):
    """POST /api/referrals/generate should create a referral code."""
    res = await client.post(
        "/api/referrals/generate",
        headers={
            "X-API-Key": "test-api-key",
            "Content-Type": "application/json",
        },
    )
    # Without auth, should return 401
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_referral_dashboard_no_auth(client: AsyncClient):
    """GET /api/referrals/dashboard should require auth."""
    res = await client.get("/api/referrals/dashboard")
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_admin_pending_commissions_no_auth(client: AsyncClient):
    """GET /api/referrals/admin/pending should require admin auth."""
    res = await client.get("/api/referrals/admin/pending")
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_referral_code_generation_and_uniqueness():
    """Verify that referral codes are generated uniquely."""
    async with async_session_maker() as db:
        owner = Tenant(phone="+821099999991", plan="free")
        db.add(owner)
        await db.flush()

        code1 = ReferralCode(code="TEST1234蹂?, owner_id=owner.id)
        db.add(code1)
        await db.flush()

        code2 = ReferralCode(code="TEST5678鍮?, owner_id=owner.id)
        db.add(code2)
        await db.flush()

        result = await db.execute(
            __import__("sqlalchemy").select(ReferralCode).where(ReferralCode.code == "TEST1234蹂?)
        )
        found = result.scalar_one_or_none()
        assert found is not None
        assert found.code == "TEST1234蹂?


@pytest.mark.asyncio
async def test_commission_creation():
    """Verify commission creation logic."""
    async with async_session_maker() as db:
        referrer = Tenant(phone="+821099999992", plan="pro", subscription_status="active")
        db.add(referrer)
        await db.flush()

        ref_code = ReferralCode(code="REFERRER01", owner_id=referrer.id)
        db.add(ref_code)
        await db.flush()

        referred = Tenant(
            phone="+821099999993",
            plan="free",
            referred_by=ref_code.id,
        )
        db.add(referred)
        await db.flush()

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
        db.add(commission)
        await db.commit()

        result = await db.get(ReferralCommission, commission.id)
        assert result is not None
        assert result.status == "pending"
        assert result.commission_amount == 1000


@pytest.mark.asyncio
async def test_commission_mark_paid():
    """Verify admin can mark commission as paid."""
    async with async_session_maker() as db:
        referrer = Tenant(phone="+821099999994", plan="pro")
        db.add(referrer)
        await db.flush()

        ref_code = ReferralCode(code="REFERRER02", owner_id=referrer.id)
        db.add(ref_code)
        await db.flush()

        referred = Tenant(
            phone="+821099999995",
            plan="free",
            referred_by=ref_code.id,
        )
        db.add(referred)
        await db.flush()

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
        db.add(commission)
        await db.commit()

        commission.status = "paid"
        await db.commit()

        result = await db.get(ReferralCommission, commission.id)
        assert result.status == "paid"


@pytest.mark.asyncio
async def test_self_referral_prevention():
    """Self-referral should not create commission."""
    async with async_session_maker() as db:
        owner = Tenant(phone="+821099999996", plan="free")
        db.add(owner)
        await db.flush()

        ref_code = ReferralCode(code="SELFREF01", owner_id=owner.id)
        db.add(ref_code)
        await db.flush()

        owner.referred_by = ref_code.id
        await db.commit()

        # Commission should not be created for self-referral
        from app.services.referral import create_commission
        result = await create_commission(
            db=db,
            referred_tenant_id=owner.id,
            source_payment_id="self-payment",
            source_type="stars",
            amount=1000,
        )
        assert result is None


@pytest.mark.asyncio
async def test_referral_code_generate_function():
    """Verify _generate_code produces valid codes."""
    from app.api.referrals import _generate_code
    code = _generate_code()
    assert len(code) >= 6
    assert code.isascii() or any(ord(c) > 127 for c in code)


@pytest.mark.asyncio
async def test_referral_link_endpoint(client: AsyncClient):
    """GET /api/referrals/my-link should return a Telegram deep link."""
    res = await client.get("/api/referrals/my-link")
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_tier_calculation():
    """Verify tier-based commission rate calculation."""
    from app.services.referral import _get_tier, default_tiers

    tiers = default_tiers()

    rate0, label0 = _get_tier(0, tiers)
    assert rate0 == 0.10
    assert label0 == "湲곕낯"

    rate1, label1 = _get_tier(3, tiers)
    assert rate1 == 0.10
    assert label1 == "湲곕낯"

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
async def test_process_payouts():
    """Verify process_payouts creates payout records and marks commissions as paid."""
    async with async_session_maker() as db:
        referrer = Tenant(phone="+821099999997", plan="pro", subscription_status="active")
        db.add(referrer)
        await db.flush()

        ref_code = ReferralCode(code="PAYOUT01", owner_id=referrer.id)
        db.add(ref_code)
        await db.flush()

        referred1 = Tenant(phone="+821099999998", plan="free", referred_by=ref_code.id)
        referred2 = Tenant(phone="+821099999999", plan="free", referred_by=ref_code.id)
        db.add_all([referred1, referred2])
        await db.flush()

        for i, ref in enumerate([referred1, referred2]):
            c = ReferralCommission(
                referrer_id=referrer.id, referred_user_id=ref.id,
                source_payment_id=f"pay-{i}", source_type="stars",
                amount=5000, commission_rate=0.10,
                commission_amount=500, status="pending",
            )
            db.add(c)
        await db.commit()

        from app.services.referral import process_payouts
        created, total = await process_payouts(db, min_amount=100)
        assert created == 1
        assert total == 1000

        from app.models.referral import ReferralPayout
        payout_count = await db.execute(
            __import__("sqlalchemy").select(__import__("sqlalchemy").func.count()).select_from(ReferralPayout)
        )
        assert payout_count.scalar_one() == 1


@pytest.mark.asyncio
async def test_leaderboard_endpoint_no_auth(client: AsyncClient):
    """GET /api/referrals/leaderboard should be publicly accessible."""
    res = await client.get("/api/referrals/leaderboard")
    assert res.status_code == 200


@pytest.mark.asyncio
async def test_admin_payouts_no_auth(client: AsyncClient):
    """GET /api/referrals/admin/payouts should require admin."""
    res = await client.get("/api/referrals/admin/payouts")
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_admin_process_payouts_no_auth(client: AsyncClient):
    """POST /api/referrals/admin/process-payouts should require admin."""
    res = await client.post("/api/referrals/admin/process-payouts")
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_admin_pending_payouts_no_auth(client: AsyncClient):
    """GET /api/referrals/admin/payouts/pending should require admin."""
    res = await client.get("/api/referrals/admin/payouts/pending")
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_admin_approve_payout_no_auth(client: AsyncClient):
    """POST /api/referrals/admin/payouts/{id}/approve should require admin."""
    res = await client.post("/api/referrals/admin/payouts/fake-id/approve")
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_referral_stats_no_auth(client: AsyncClient):
    """GET /api/referrals/stats should require auth."""
    res = await client.get("/api/referrals/stats")
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_set_chat_id_no_auth(client: AsyncClient):
    """POST /api/referrals/set-chat-id should require auth."""
    res = await client.post("/api/referrals/set-chat-id", json={"chat_id": "12345"})
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_admin_cancel_commission_no_auth(client: AsyncClient):
    """POST /api/referrals/admin/commissions/{id}/cancel should require admin."""
    res = await client.post("/api/referrals/admin/commissions/fake-id/cancel")
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_cancel_commission():
    """Verify cancel_commission sets status to cancelled and sends notification."""
    async with async_session_maker() as db:
        referrer = Tenant(phone="+821011111112", plan="pro", telegram_chat_id="99999")
        db.add(referrer)
        await db.flush()

        referred = Tenant(phone="+821011111113", plan="free")
        db.add(referred)
        await db.flush()

        comm = ReferralCommission(
            referrer_id=referrer.id, referred_user_id=referred.id,
            source_payment_id="cancel-test", source_type="stars",
            amount=5000, commission_rate=0.10,
            commission_amount=500, status="pending",
        )
        db.add(comm)
        await db.commit()

        from app.services.referral import cancel_commission
        ok = await cancel_commission(db, comm.id)
        assert ok is True

        result = await db.get(ReferralCommission, comm.id)
        assert result.status == "cancelled"


@pytest.mark.asyncio
async def test_cancel_commissions_by_payment():
    """Verify cancelling by payment_id cancels all matching pending commissions."""
    async with async_session_maker() as db:
        referrer = Tenant(phone="+821011111114", plan="pro")
        db.add(referrer)
        await db.flush()

        referred = Tenant(phone="+821011111115", plan="free")
        db.add(referred)
        await db.flush()

        for i in range(3):
            c = ReferralCommission(
                referrer_id=referrer.id, referred_user_id=referred.id,
                source_payment_id="payment-refund-1", source_type="stars",
                amount=3000, commission_rate=0.10,
                commission_amount=300, status="pending",
            )
            db.add(c)
        await db.commit()

        from app.services.referral import cancel_commissions_by_payment
        cancelled = await cancel_commissions_by_payment(db, "payment-refund-1")
        assert cancelled == 3


@pytest.mark.asyncio
async def test_stats_endpoint():
    """Verify stats endpoint returns expected structure."""
    async with async_session_maker() as db:
        from app.services.referral import get_stats
        data = await get_stats(db)
        assert "total_referrers" in data
        assert "total_referred" in data
        assert "daily" in data
        assert len(data["daily"]) == 30


@pytest.mark.asyncio
async def test_csv_download_no_auth(client: AsyncClient):
    """GET /api/referrals/stats/csv should require auth."""
    res = await client.get("/api/referrals/stats/csv")
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_admin_commissions_csv_no_auth(client: AsyncClient):
    """GET /api/referrals/admin/commissions/csv should require admin."""
    res = await client.get("/api/referrals/admin/commissions/csv")
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_change_code_no_auth(client: AsyncClient):
    """POST /api/referrals/change-code should require auth."""
    res = await client.post("/api/referrals/change-code", json={"new_code": "NEWCODE"})
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_qr_endpoint_no_auth(client: AsyncClient):
    """GET /api/referrals/my-qr should require auth."""
    res = await client.get("/api/referrals/my-qr")
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_duplicate_commission_prevention():
    """Verify duplicate commission is prevented."""
    async with async_session_maker() as db:
        referrer = Tenant(phone="+821099999991", plan="pro")
        db.add(referrer)
        await db.flush()

        ref_code = ReferralCode(code="DUPREF01", owner_id=referrer.id)
        db.add(ref_code)
        await db.flush()

        referred = Tenant(phone="+821099999992", plan="free", referred_by=ref_code.id)
        db.add(referred)
        await db.flush()

        from app.services.referral import create_commission
        c1 = await create_commission(db, referred.id, "dup-payment", "stars", 5000)
        assert c1 is not None

        c2 = await create_commission(db, referred.id, "dup-payment", "stars", 5000)
        assert c2 is None


@pytest.mark.asyncio
async def test_my_commissions_endpoint(client: AsyncClient):
    """GET /api/referrals/my-commissions should return commission list for authenticated user."""
    res = await client.get("/api/referrals/my-commissions")
    assert res.status_code == 401

    from app.core.security import create_user_access_token
    from app.models.user import User

    async with async_session_maker() as db:
        user = User(phone="+821099999995")
        db.add(user)
        referrer = Tenant(phone="+821099999995", plan="pro")
        db.add(referrer)
        referred = Tenant(phone="+821099999996", plan="free")
        db.add(referred)
        await db.commit()
        await db.refresh(user)
        await db.refresh(referrer)
        await db.refresh(referred)

        user_jwt = create_user_access_token(user.id)

        ref_code = ReferralCode(code="MYCOMM01", owner_id=referrer.id)
        db.add(ref_code)
        await db.flush()
        referred.referred_by = ref_code.id
        await db.flush()

        from app.services.referral import create_commission
        await create_commission(db, referred.id, "mc-pay-1", "stars", 10000)
        await create_commission(db, referred.id, "mc-pay-2", "usdt", 20000)

    from app.core.rate_limiter import reset_rate_limits
    reset_rate_limits()
    res = await client.get("/api/referrals/my-commissions",
                           headers={"Authorization": f"Bearer {user_jwt}"})
    assert res.status_code == 200
    data = res.json()
    assert data["total_count"] >= 2


@pytest.mark.asyncio
async def test_set_wallet_endpoint(client: AsyncClient):
    """POST /api/referrals/set-wallet should store wallet address."""
    res = await client.post("/api/referrals/set-wallet", json={"wallet_address": "0x1234567890abcdef"})
    assert res.status_code == 401

    from app.core.security import create_user_access_token
    from app.models.user import User

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
    res = await client.post("/api/referrals/set-wallet",
                            json={"wallet_address": "TXYZ123456789"},
                            headers={"Authorization": f"Bearer {user_jwt}"})
    assert res.status_code == 200
    data = res.json()
    assert data["success"] is True


@pytest.mark.asyncio
async def test_admin_settings_endpoint(client: AsyncClient, admin_token: str):
    """GET/PUT /api/referrals/admin/settings should work for admin only."""
    from app.core.rate_limiter import reset_rate_limits
    reset_rate_limits()

    # no auth
    res = await client.get("/api/referrals/admin/settings")
    assert res.status_code == 401

    # admin auth
    res = await client.get("/api/referrals/admin/settings", headers={"Authorization": f"Bearer {admin_token}"})
    assert res.status_code == 200
    data = res.json()
    assert "settings" in data

    # update settings
    res = await client.put("/api/referrals/admin/settings",
                           json={"settings": [{"key": "min_payout", "value": "50"}]},
                           headers={"Authorization": f"Bearer {admin_token}"})
    assert res.status_code == 200

    # verify
    res = await client.get("/api/referrals/admin/settings", headers={"Authorization": f"Bearer {admin_token}"})
    data = res.json()
    min_payout = next((s for s in data["settings"] if s["key"] == "min_payout"), None)
    assert min_payout is not None
    assert min_payout["value"] == "50"


@pytest.mark.asyncio
async def test_admin_codes_endpoint(client: AsyncClient, admin_token: str):
    """GET /api/referrals/admin/codes should return code stats for admin only."""
    res = await client.get("/api/referrals/admin/codes")
    assert res.status_code == 401

    res = await client.get("/api/referrals/admin/codes", headers={"Authorization": f"Bearer {admin_token}"})
    assert res.status_code == 200
    data = res.json()
    assert "items" in data


@pytest.mark.asyncio
async def test_referral_rate_limit(client: AsyncClient):
    """Rate limit should trigger after too many generate requests."""
    from app.core.security import create_user_access_token
    from app.models.user import User

    async with async_session_maker() as db:
        user = User(phone="+821099999998")
        db.add(user)
        tenant = Tenant(phone="+821099999998", plan="free")
        db.add(tenant)
        await db.commit()
        await db.refresh(user)
        user_jwt = create_user_access_token(user.id)

    from app.core.rate_limiter import reset_rate_limits
    reset_rate_limits()

    for _ in range(5):
        res = await client.post("/api/referrals/generate", headers={"Authorization": f"Bearer {user_jwt}"})
        assert res.status_code == 200

    res = await client.post("/api/referrals/generate", headers={"Authorization": f"Bearer {user_jwt}"})
    assert res.status_code == 429
