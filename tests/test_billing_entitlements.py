"""Audit fix batch 1 — entitlement enforcement (C2), subscription lapse/cancel
revocation (C3), Stars balance bypass (H2), unverified USDT admin-confirm (H4).
"""

import itertools

import pytest

from app.api.deps import Identity, get_current_identity
from app.database import async_session_maker
from app.main import app
from app.models.tenant import PaymentRecord, Tenant
from app.services.usage_tracker import apply_plan_limits

_phone_seq = itertools.count(1)


async def _make_tenant(db, *, plan="free", **overrides):
    tenant = Tenant(phone=overrides.pop("phone", f"+8219{next(_phone_seq):08d}"))
    db.add(tenant)
    await db.flush()
    await apply_plan_limits(db, tenant, plan)
    for key, value in overrides.items():
        setattr(tenant, key, value)
    await db.commit()
    await db.refresh(tenant)
    return tenant


def _as_tenant(client, tenant_id: str):
    """Override the resolved identity to a non-admin user scoped to this tenant."""
    app.dependency_overrides[get_current_identity] = lambda: Identity(kind="user", tenant_id=tenant_id)


# ─── C2: account capacity ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_account_blocked_at_plan_limit(client, db_session):
    tenant = await _make_tenant(db_session, plan="free")  # max_accounts=1
    _as_tenant(client, tenant.id)

    first = await client.post("/api/accounts", json={"phone": "+821090000001"})
    assert first.status_code == 201

    second = await client.post("/api/accounts", json={"phone": "+821090000002"})
    assert second.status_code == 403
    assert "한도" in second.json()["detail"]


@pytest.mark.asyncio
async def test_create_account_allowed_under_plan_limit(client, db_session):
    tenant = await _make_tenant(db_session, plan="pro")  # max_accounts=10
    _as_tenant(client, tenant.id)

    res = await client.post("/api/accounts", json={"phone": "+821090000003"})
    assert res.status_code == 201


@pytest.mark.asyncio
async def test_create_account_admin_bypasses_capacity_check(client, db_session):
    # default `client` fixture identity is admin — no tenant, no cap.
    res = await client.post("/api/accounts", json={"phone": "+821090000004"})
    assert res.status_code == 201


# ─── C2: broadcast capacity ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_broadcast_blocked_when_plan_disallows(client, db_session):
    tenant = await _make_tenant(db_session, plan="free")  # can_broadcast=False
    _as_tenant(client, tenant.id)

    account = await client.post("/api/accounts", json={"phone": "+821090000005"})
    account_id = account.json()["id"]

    res = await client.post(
        "/api/broadcast",
        data={"account_id": account_id, "message": "hi", "recipients": "[]"},
    )
    assert res.status_code == 403
    assert "발송 기능" in res.json()["detail"]


@pytest.mark.asyncio
async def test_create_broadcast_allowed_when_plan_permits(client, db_session):
    tenant = await _make_tenant(db_session, plan="pro")  # can_broadcast=True
    _as_tenant(client, tenant.id)

    account = await client.post("/api/accounts", json={"phone": "+821090000006"})
    account_id = account.json()["id"]

    res = await client.post(
        "/api/broadcast",
        data={"account_id": account_id, "message": "hi", "recipients": '["-100123"]'},
    )
    assert res.status_code == 202


# ─── C3: expired/canceled subscriptions get downgraded ──────────────────


@pytest.mark.asyncio
async def test_downgrade_expired_tenants_reverts_canceled_past_period(db_session, monkeypatch):
    import app.services.billing as billing_module

    monkeypatch.setattr(billing_module, "async_session_maker", lambda: db_session_cm(db_session))

    tenant = await _make_tenant(
        db_session,
        plan="team",
        subscription_status="canceled",
        billing_period_end=billing_module.utcnow_naive().replace(year=2020),
    )

    result = await billing_module.downgrade_expired_tenants()

    await db_session.refresh(tenant)
    assert tenant.id in result["tenant_ids"]
    assert tenant.plan == "free"
    assert tenant.max_accounts == 1
    assert tenant.can_broadcast is False
    assert tenant.subscription_status == "canceled"


@pytest.mark.asyncio
async def test_downgrade_expired_tenants_reverts_lapsed_active_subscription(db_session, monkeypatch):
    """A tenant who never explicitly canceled but whose billing period simply ran out
    (manual USDT payments don't auto-renew) must also lose paid-tier access."""
    import app.services.billing as billing_module

    monkeypatch.setattr(billing_module, "async_session_maker", lambda: db_session_cm(db_session))

    tenant = await _make_tenant(
        db_session,
        plan="pro",
        subscription_status="active",
        billing_period_end=billing_module.utcnow_naive().replace(year=2020),
    )

    result = await billing_module.downgrade_expired_tenants()

    await db_session.refresh(tenant)
    assert tenant.id in result["tenant_ids"]
    assert tenant.plan == "free"
    assert tenant.subscription_status == "expired"


@pytest.mark.asyncio
async def test_downgrade_expired_tenants_leaves_active_period_untouched(db_session, monkeypatch):
    import app.services.billing as billing_module

    monkeypatch.setattr(billing_module, "async_session_maker", lambda: db_session_cm(db_session))

    tenant = await _make_tenant(
        db_session,
        plan="pro",
        subscription_status="active",
        billing_period_end=billing_module.utcnow_naive().replace(year=2099),
    )

    result = await billing_module.downgrade_expired_tenants()

    await db_session.refresh(tenant)
    assert tenant.id not in result["tenant_ids"]
    assert tenant.plan == "pro"


class db_session_cm:
    """Wrap an already-open test db_session as an async-context-manager, matching
    async_session_maker()'s call signature, so downgrade_expired_tenants (which opens
    its own session) reuses the same in-test transaction/engine."""

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


# ─── H2: Stars spend must not bypass balance check ──────────────────────


@pytest.mark.asyncio
async def test_process_stars_payment_rejects_insufficient_balance(db_session, monkeypatch):
    import app.services.billing as billing_module

    monkeypatch.setattr(billing_module, "async_session_maker", lambda: db_session_cm(db_session))

    tenant = await _make_tenant(db_session, plan="free", stars_balance=10)

    result = await billing_module.process_stars_payment(tenant.id, "extra_account_slot", 150)

    assert result["success"] is False
    await db_session.refresh(tenant)
    assert tenant.stars_balance == 10  # untouched


@pytest.mark.asyncio
async def test_process_stars_payment_deducts_on_sufficient_balance(db_session, monkeypatch):
    import app.services.billing as billing_module

    monkeypatch.setattr(billing_module, "async_session_maker", lambda: db_session_cm(db_session))

    tenant = await _make_tenant(db_session, plan="free", stars_balance=200)

    result = await billing_module.process_stars_payment(tenant.id, "extra_account_slot", 150)

    assert result["success"] is True
    await db_session.refresh(tenant)
    assert tenant.stars_balance == 50


# ─── H4: USDT admin-confirm must verify against real chain data ─────────


@pytest.mark.asyncio
async def test_confirm_usdt_payment_rejects_unverified_tx_hash(db_session, monkeypatch):
    import app.services.billing as billing_module

    monkeypatch.setattr(billing_module, "async_session_maker", lambda: db_session_cm(db_session))

    async def fake_no_transactions():
        return []

    import app.services.usdt_watcher as watcher_module
    monkeypatch.setattr(watcher_module, "get_usdt_transactions", fake_no_transactions)

    tenant = await _make_tenant(db_session, plan="free")

    result = await billing_module.confirm_usdt_payment(tenant.id, "fabricated-tx-hash")

    assert result["success"] is False
    await db_session.refresh(tenant)
    assert tenant.plan == "free"
    assert tenant.subscription_status != "active"


@pytest.mark.asyncio
async def test_confirm_usdt_payment_activates_on_verified_tx_hash(db_session, monkeypatch):
    import app.services.billing as billing_module

    monkeypatch.setattr(billing_module, "async_session_maker", lambda: db_session_cm(db_session))

    async def fake_transactions():
        return [{
            "tx_id": "real-tx-abc123",
            "from_address": "Txxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            "amount_usdt": 100.0,
            "amount_cents": 10000,
            "block_timestamp": 1234567890,
            "memo": "",
        }]

    import app.services.usdt_watcher as watcher_module
    monkeypatch.setattr(watcher_module, "get_usdt_transactions", fake_transactions)

    tenant = await _make_tenant(db_session, plan="pro")

    result = await billing_module.confirm_usdt_payment(tenant.id, "real-tx-abc123")

    assert result["success"] is True
    await db_session.refresh(tenant)
    assert tenant.subscription_status == "active"

    from sqlalchemy import select
    rec = (await db_session.execute(
        select(PaymentRecord).where(PaymentRecord.tx_id == "real-tx-abc123")
    )).scalar_one_or_none()
    assert rec is not None
    assert rec.tenant_id == tenant.id


@pytest.mark.asyncio
async def test_confirm_usdt_payment_rejects_reused_tx_hash(db_session, monkeypatch):
    import app.services.billing as billing_module

    monkeypatch.setattr(billing_module, "async_session_maker", lambda: db_session_cm(db_session))

    async def fake_transactions():
        return [{
            "tx_id": "already-used-tx",
            "from_address": "Txxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            "amount_usdt": 100.0,
            "amount_cents": 10000,
            "block_timestamp": 1234567890,
            "memo": "",
        }]

    import app.services.usdt_watcher as watcher_module
    monkeypatch.setattr(watcher_module, "get_usdt_transactions", fake_transactions)

    tenant_a = await _make_tenant(db_session, plan="pro")
    tenant_b = await _make_tenant(db_session, plan="pro")

    first = await billing_module.confirm_usdt_payment(tenant_a.id, "already-used-tx")
    assert first["success"] is True

    second = await billing_module.confirm_usdt_payment(tenant_b.id, "already-used-tx")
    assert second["success"] is False
