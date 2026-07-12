"""Test USDT payment phone identity — paid users get a recoverable User record.

Verifies that request_api_key creates a User record when a phone is provided.
"""

import pytest
from sqlalchemy import select

from app.models.user import User
from app.models.tenant import Tenant


@pytest.mark.asyncio
async def test_request_key_with_phone_creates_user(db_session, monkeypatch):
    """request_key with phone creates a User record visible in the same session."""
    import app.api.usdt_payment as m
    from tests.test_billing_entitlements import db_session_cm
    monkeypatch.setattr(m, "async_session_maker", lambda: db_session_cm(db_session))

    result = await m.request_api_key(plan="pro", phone="+821099990001")

    assert result["success"] is True

    # Expire all so next reads go to DB (handler committed inside same session)
    db_session.expire_all()

    user = (await db_session.execute(select(User).where(User.phone == "+821099990001"))).scalar_one_or_none()
    assert user is not None, "User record must be created for paid signup with phone"
    assert user.phone == "+821099990001"

    tenant = (await db_session.execute(select(Tenant).where(Tenant.phone == "+821099990001"))).scalar_one_or_none()
    assert tenant is not None
    assert tenant.plan == "pro"


@pytest.mark.asyncio
async def test_request_key_without_phone_no_user(db_session, monkeypatch):
    """request_key without phone must NOT create a User (backward compat)."""
    import app.api.usdt_payment as m
    from tests.test_billing_entitlements import db_session_cm
    monkeypatch.setattr(m, "async_session_maker", lambda: db_session_cm(db_session))

    result = await m.request_api_key(plan="pro")

    assert result["success"] is True
    payment_ref = result["payment_ref"]

    db_session.expire_all()

    tenant = (await db_session.execute(select(Tenant).where(Tenant.payment_ref == payment_ref))).scalar_one_or_none()
    assert tenant is not None
    assert tenant.phone == f"pending-{payment_ref}"

    user = (await db_session.execute(select(User).where(User.phone == f"pending-{payment_ref}"))).scalar_one_or_none()
    assert user is None, "No User record should be created when phone is absent"


@pytest.mark.asyncio
async def test_request_key_with_phone_no_duplicate_user(db_session, monkeypatch):
    """When called twice with same phone, only one User record is created."""
    import app.api.usdt_payment as m
    from tests.test_billing_entitlements import db_session_cm
    monkeypatch.setattr(m, "async_session_maker", lambda: db_session_cm(db_session))

    r1 = await m.request_api_key(plan="pro", phone="+821099990002")
    assert r1["success"] is True

    m._request_timestamps.clear()

    r2 = await m.request_api_key(plan="pro", phone="+821099990002")
    assert r2["success"] is True

    db_session.expire_all()

    users = (await db_session.execute(select(User).where(User.phone == "+821099990002"))).scalars().all()
    assert len(users) == 1


@pytest.mark.asyncio
async def test_request_key_with_phone_then_login_flow(db_session, monkeypatch):
    """User created by paid flow can be looked up by phone (simulating login)."""
    import app.api.usdt_payment as m
    from app.crud import user as user_crud
    from tests.test_billing_entitlements import db_session_cm
    monkeypatch.setattr(m, "async_session_maker", lambda: db_session_cm(db_session))

    await m.request_api_key(plan="pro", phone="+821099990010")

    db_session.expire_all()

    user = await user_crud.get_user_by_phone(db_session, "+821099990010")
    assert user is not None
    assert user.phone == "+821099990010"


@pytest.mark.asyncio
async def test_request_key_rejects_free_plan(db_session, monkeypatch):
    """Free plan is rejected (trial-only, not purchasable via USDT)."""
    import app.api.usdt_payment as m
    from tests.test_billing_entitlements import db_session_cm
    monkeypatch.setattr(m, "async_session_maker", lambda: db_session_cm(db_session))

    with pytest.raises(Exception) as exc:
        await m.request_api_key(plan="free", phone="+821099990011")
    assert "무료 체험" in str(exc.value)