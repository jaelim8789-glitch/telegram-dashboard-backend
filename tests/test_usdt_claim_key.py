"""USDT payment → claim-key → login-with-api-key E2E tests.

Focuses on the ``GET /api/payment/claim-key/{payment_ref}`` one-time key retrieval
endpoint and the watcher's ``process_incoming_tx`` / admin ``confirm_usdt_payment``
producing keys that work with ``login-with-api-key``.

Actual USDT transaction detection (Trongrid) is mocked; we test the contract
between the watcher/confirm step and the claim-key endpoint.
"""

import pytest

from app.core.security import hash_api_key
from app.crud import user as user_crud
from app.models.api_key import APIKey
from app.models.tenant import PaymentRecord, Tenant
from app.models.user import User


pytestmark = pytest.mark.asyncio


async def _setup_paid_tenant_and_key(
    db_session,
    phone: str = "+821099991100",
    tenant_plan: str = "pro",
) -> tuple[Tenant, str]:
    """Create a paid tenant with an activated API key (simulating watcher output)."""
    from app.core.security import generate_user_api_key

    raw_key = generate_user_api_key()
    user = User(phone=phone, api_key_hash=hash_api_key(raw_key))
    db_session.add(user)
    await db_session.flush()

    tenant = Tenant(
        phone=phone,
        plan=tenant_plan,
        subscription_status="active",
        payment_ref="TM-TEST001",
    )
    db_session.add(tenant)
    await db_session.flush()

    api_key = APIKey(key=raw_key, name=f"USDT-{tenant_plan}-test", is_active=True, tenant_id=tenant.id)
    db_session.add(api_key)
    await db_session.flush()

    db_session.add(PaymentRecord(
        tx_id="mock_tx_001",
        tenant_id=tenant.id,
        from_address="TTestAddress",
        amount_usdt=10000,  # $100.00
        plan=tenant_plan,
        status="completed",
        api_key_id=api_key.id,
        claimed=False,
    ))
    await db_session.commit()
    return tenant, raw_key


# ── 1. Paid user gets one usable raw key ─────────────────────────────────────


async def test_claim_key_returns_raw_key(unauthenticated_client, db_session):
    tenant, raw_key = await _setup_paid_tenant_and_key(db_session)

    res = await unauthenticated_client.get(f"/api/payment/claim-key/{tenant.payment_ref}")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "success"
    assert body["api_key"] == raw_key


# ── 2. Second claim cannot retrieve raw key ──────────────────────────────────


async def test_claim_key_second_call_returns_already_claimed(unauthenticated_client, db_session):
    tenant, raw_key = await _setup_paid_tenant_and_key(db_session)

    res1 = await unauthenticated_client.get(f"/api/payment/claim-key/{tenant.payment_ref}")
    assert res1.status_code == 200
    assert res1.json()["api_key"] == raw_key

    res2 = await unauthenticated_client.get(f"/api/payment/claim-key/{tenant.payment_ref}")
    assert res2.status_code == 200
    assert res2.json()["status"] == "already_claimed"
    assert "api_key" not in res2.json()


# ── 3. Claimed key works with login-with-api-key ─────────────────────────────


async def test_claimed_key_works_with_login(unauthenticated_client, db_session):
    tenant, raw_key = await _setup_paid_tenant_and_key(db_session)

    claim_res = await unauthenticated_client.get(f"/api/payment/claim-key/{tenant.payment_ref}")
    assert claim_res.status_code == 200
    assert claim_res.json()["api_key"] == raw_key

    login_res = await unauthenticated_client.post(
        "/api/auth/login-with-api-key",
        json={"api_key": raw_key},
    )
    assert login_res.status_code == 200
    assert login_res.json()["token_type"] == "bearer"
    assert login_res.json()["access_token"]

    me_res = await unauthenticated_client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {login_res.json()['access_token']}"},
    )
    assert me_res.status_code == 200
    assert me_res.json()["role"] == "user"
    assert me_res.json()["phone"] == tenant.phone


# ── 4. Watcher cannot create duplicate issuance for the same transaction ──────


async def test_duplicate_tx_id_is_rejected(unauthenticated_client, db_session):
    from sqlalchemy import select

    # First record
    db_session.add(PaymentRecord(
        tx_id="dup_tx_id",
        tenant_id="tenant-dup-test",
        from_address="TTest",
        amount_usdt=5000,
        plan="pro",
        status="completed",
        api_key_id="key-dup-test",
    ))
    await db_session.commit()

    # Second attempt with same tx_id should fail UNIQUE constraint
    from sqlalchemy.exc import IntegrityError
    with pytest.raises(IntegrityError):
        db_session.add(PaymentRecord(
            tx_id="dup_tx_id",
            tenant_id="tenant-dup-test-2",
            from_address="TTest",
            amount_usdt=5000,
            plan="pro",
            status="completed",
        ))
        await db_session.commit()


# ── 5. Raw key is never logged ──────────────────────────────────────────────


async def test_no_raw_key_in_log_output(unauthenticated_client, db_session, caplog):
    import logging
    caplog.set_level(logging.INFO)

    tenant, raw_key = await _setup_paid_tenant_and_key(db_session)

    res = await unauthenticated_client.get(f"/api/payment/claim-key/{tenant.payment_ref}")
    assert res.status_code == 200
    assert res.json()["api_key"] == raw_key

    # The raw key must not appear in any log record
    log_text = " ".join(r.message for r in caplog.records)
    assert raw_key not in log_text
    assert "sk-" not in log_text


# ── 6. Admin-confirmed payment also produces a usable login key ──────────────


async def test_admin_confirmed_user_can_login(unauthenticated_client, db_session):
    """Simulate admin confirm_usdt_payment: tenant activated + APIKey created +
    user.api_key_hash set → user logs in with login-with-api-key."""
    from app.core.security import generate_user_api_key, hash_api_key

    phone = "+821099991101"
    user = User(phone=phone)
    db_session.add(user)
    await db_session.flush()

    tenant = Tenant(
        phone=phone,
        plan="team",
        subscription_status="active",
        payment_ref="TM-CONFIRM-TEST",
    )
    db_session.add(tenant)
    await db_session.flush()

    # Simulate confirm_usdt_payment logic: create APIKey + set api_key_hash
    raw_key = generate_user_api_key()
    api_key = APIKey(key=raw_key, name="USDT-team-admin-confirm", is_active=True, tenant_id=tenant.id)
    db_session.add(api_key)
    await db_session.flush()

    user.api_key_hash = hash_api_key(raw_key)
    await db_session.flush()

    db_session.add(PaymentRecord(
        tx_id="confirm_tx_001",
        tenant_id=tenant.id,
        from_address="TConfirmAddr",
        amount_usdt=19900,
        plan="team",
        status="completed",
        api_key_id=api_key.id,
    ))
    await db_session.commit()

    # User can login with the key
    login_res = await unauthenticated_client.post(
        "/api/auth/login-with-api-key",
        json={"api_key": raw_key},
    )
    assert login_res.status_code == 200
    assert login_res.json()["token_type"] == "bearer"

    # Claim the key (one-time)
    claim_res = await unauthenticated_client.get(f"/api/payment/claim-key/{tenant.payment_ref}")
    assert claim_res.status_code == 200
    assert claim_res.json()["api_key"] == raw_key
