"""Native Telegram Stars top-up — credit_stars_from_telegram_payment.

Covers the one thing that actually matters for a payment credit path:
idempotency on telegram_payment_charge_id (Telegram can redeliver the
successful_payment update), plus the basic credit-and-record behavior.
"""

import itertools

import pytest

from app.models.tenant import Tenant
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


class db_session_cm:
    """Wrap an already-open test db_session as an async-context-manager, matching
    async_session_maker()'s call signature — see test_billing_entitlements.py."""

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_credit_stars_from_telegram_payment_adds_to_balance(db_session, monkeypatch):
    import app.services.billing as billing_module

    monkeypatch.setattr(billing_module, "async_session_maker", lambda: db_session_cm(db_session))

    tenant = await _make_tenant(db_session, plan="free", stars_balance=10)

    result = await billing_module.credit_stars_from_telegram_payment(tenant.id, 500, "charge-abc-1")

    assert result["success"] is True
    assert result["stars_balance"] == 510
    await db_session.refresh(tenant)
    assert tenant.stars_balance == 510


@pytest.mark.asyncio
async def test_credit_stars_from_telegram_payment_is_idempotent_on_charge_id(db_session, monkeypatch):
    """Telegram can redeliver the successful_payment update on retry — a repeat
    charge id must not double-credit the tenant."""
    import app.services.billing as billing_module

    monkeypatch.setattr(billing_module, "async_session_maker", lambda: db_session_cm(db_session))

    tenant = await _make_tenant(db_session, plan="free", stars_balance=0)

    first = await billing_module.credit_stars_from_telegram_payment(tenant.id, 1000, "charge-dup-1")
    second = await billing_module.credit_stars_from_telegram_payment(tenant.id, 1000, "charge-dup-1")

    assert first["success"] is True
    assert second == {"success": True, "already_processed": True}
    await db_session.refresh(tenant)
    assert tenant.stars_balance == 1000


@pytest.mark.asyncio
async def test_credit_stars_from_telegram_payment_unknown_tenant(db_session, monkeypatch):
    import app.services.billing as billing_module

    monkeypatch.setattr(billing_module, "async_session_maker", lambda: db_session_cm(db_session))

    result = await billing_module.credit_stars_from_telegram_payment("nonexistent-tenant", 100, "charge-none")

    assert result["success"] is False
