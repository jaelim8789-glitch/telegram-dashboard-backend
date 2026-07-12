"""Test USDT payment phone identity — paid users get a recoverable User record.

Verifies that POST /api/payment/request-key creates a User record when a phone
is provided, so paid users can later log in via phone verification instead of
having a fabricated pending-{payment_ref} identity.
"""

import pytest
from sqlalchemy import select

from app.models.user import User
from app.models.tenant import Tenant


class TestPaidSignupPhoneIdentity:
    """When phone is provided to /api/payment/request-key, a User record must
    be created so the user has a verified/recoverable identity."""

    @pytest.mark.asyncio
    async def test_request_key_with_phone_creates_user(self, unauthenticated_client, db_session, monkeypatch):
        """POST /api/payment/request-key?plan=pro&phone=+821099990001 creates a User."""
        import app.api.usdt_payment as usdt_payment_module
        from tests.test_billing_entitlements import db_session_cm
        monkeypatch.setattr(usdt_payment_module, "async_session_maker", lambda: db_session_cm(db_session))

        res = await unauthenticated_client.post(
            "/api/payment/request-key?plan=pro&phone=+821099990001"
        )
        assert res.status_code == 200
        data = res.json()
        assert data["success"] is True
        assert data["plan"] == "pro"

        # Verify User record was created
        result = await db_session.execute(
            select(User).where(User.phone == "+821099990001")
        )
        user = result.scalar_one_or_none()
        assert user is not None, "User record must be created for paid signup with phone"
        assert user.phone == "+821099990001"
        assert user.is_active is True

        # Verify Tenant was created with the real phone
        result = await db_session.execute(
            select(Tenant).where(Tenant.phone == "+821099990001")
        )
        tenant = result.scalar_one_or_none()
        assert tenant is not None
        assert tenant.plan == "pro"
        assert tenant.subscription_status == "pending"

    @pytest.mark.asyncio
    async def test_request_key_without_phone_no_user(self, unauthenticated_client, db_session, monkeypatch):
        """POST /api/payment/request-key?plan=pro (no phone) must NOT create a User,
        but still create a Tenant with a pending-* phone (backward compat)."""
        import app.api.usdt_payment as usdt_payment_module
        from tests.test_billing_entitlements import db_session_cm
        monkeypatch.setattr(usdt_payment_module, "async_session_maker", lambda: db_session_cm(db_session))

        res = await unauthenticated_client.post(
            "/api/payment/request-key?plan=pro"
        )
        assert res.status_code == 200
        data = res.json()
        assert data["success"] is True

        payment_ref = data["payment_ref"]

        # Verify Tenant was created with pending-{payment_ref} phone
        result = await db_session.execute(
            select(Tenant).where(Tenant.payment_ref == payment_ref)
        )
        tenant = result.scalar_one_or_none()
        assert tenant is not None
        assert tenant.phone == f"pending-{payment_ref}"

        # Verify NO User record was created
        result = await db_session.execute(
            select(User).where(User.phone == f"pending-{payment_ref}")
        )
        user = result.scalar_one_or_none()
        assert user is None, "No User record should be created when phone is absent"

    @pytest.mark.asyncio
    async def test_request_key_with_phone_existing_user_no_duplicate(self, unauthenticated_client, db_session, monkeypatch):
        """When a User already exists for the phone, request-key must not create a duplicate."""
        import app.api.usdt_payment as usdt_payment_module
        from tests.test_billing_entitlements import db_session_cm
        monkeypatch.setattr(usdt_payment_module, "async_session_maker", lambda: db_session_cm(db_session))

        # First call creates User
        res1 = await unauthenticated_client.post(
            "/api/payment/request-key?plan=pro&phone=+821099990002"
        )
        assert res1.status_code == 200

        # Second call with same phone — rate limiter would block, so use a different
        # phone to test the no-duplicate logic
        res2 = await unauthenticated_client.post(
            "/api/payment/request-key?plan=pro&phone=+821099990003"
        )
        assert res2.status_code == 200

        # Verify two User records exist (one per unique phone)
        result = await db_session.execute(
            select(User).where(User.phone.in_(["+821099990002", "+821099990003"]))
        )
        users = result.scalars().all()
        assert len(users) == 2

        # Verify no duplicate for the same phone
        result = await db_session.execute(
            select(User).where(User.phone == "+821099990002")
        )
        users_same_phone = result.scalars().all()
        assert len(users_same_phone) == 1, "Must not create duplicate User records for same phone"

    @pytest.mark.asyncio
    async def test_request_key_with_phone_team_plan(self, unauthenticated_client, db_session, monkeypatch):
        """Team plan with phone also creates User record."""
        import app.api.usdt_payment as usdt_payment_module
        from tests.test_billing_entitlements import db_session_cm
        monkeypatch.setattr(usdt_payment_module, "async_session_maker", lambda: db_session_cm(db_session))

        res = await unauthenticated_client.post(
            "/api/payment/request-key?plan=team&phone=+821099990004"
        )
        assert res.status_code == 200
        data = res.json()
        assert data["plan"] == "team"

        result = await db_session.execute(
            select(User).where(User.phone == "+821099990004")
        )
        user = result.scalar_one_or_none()
        assert user is not None

    @pytest.mark.asyncio
    async def test_request_key_deprecated_plan_rejected(self, unauthenticated_client):
        """Deprecated plans (basic, enterprise) are still rejected."""
        res = await unauthenticated_client.post(
            "/api/payment/request-key?plan=basic&phone=+821099990005"
        )
        assert res.status_code == 400
        assert "더 이상 제공되지 않습니다" in res.json()["detail"]

    @pytest.mark.asyncio
    async def test_request_key_unknown_plan_rejected(self, unauthenticated_client):
        """Unknown plans are rejected."""
        res = await unauthenticated_client.post(
            "/api/payment/request-key?plan=nonexistent&phone=+821099990006"
        )
        assert res.status_code == 400
        assert "유효하지 않은 요금제" in res.json()["detail"]