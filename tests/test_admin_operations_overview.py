"""Admin Operations Center overview endpoint tests.

Covers the single aggregated snapshot endpoint used by the admin dashboard:
  - account status bucketing (connected / floodwait / need_login / banned)
  - 30-day revenue trend aggregation
  - active subscription count
  - scheduler / redis / health sub-blocks present
"""

import pytest

from app.config import settings
from app.core.security import create_access_token

pytestmark = pytest.mark.asyncio


def _admin_headers() -> dict:
    return {"Authorization": f"Bearer {create_access_token()}"}


async def _wire_db(unauthenticated_client, db_session):
    from app.main import app
    import app.database as db_mod
    from app.api.admin import get_db as admin_get_db
    from app.api.deps import Identity

    async def _override():
        yield db_session

    async def _admin_identity_override():
        return Identity(kind="admin")

    app.dependency_overrides[db_mod.get_db] = _override
    app.dependency_overrides[admin_get_db] = _override
    from app.api import deps as deps_mod
    app.dependency_overrides[deps_mod.get_current_identity] = _admin_identity_override
    return app


def _unwire_db(app):
    import app.database as db_mod
    from app.api.admin import get_db as admin_get_db
    from app.api import deps as deps_mod
    app.dependency_overrides.pop(db_mod.get_db, None)
    app.dependency_overrides.pop(admin_get_db, None)
    app.dependency_overrides.pop(deps_mod.get_current_identity, None)


async def _seed_data(db_session):
    from datetime import datetime, timedelta
    from app.models.account import Account
    from app.models.tenant import PaymentRecord, Tenant

    # Connected account
    db_session.add(Account(phone="+821010000001", status="active", session_data="encrypted-session-1", tenant_id="t-1"))
    # FloodWait account
    db_session.add(Account(phone="+821010000002", status="active", session_data="encrypted-session-2", tenant_id="t-1", last_error="FLOOD_WAIT 30"))
    # Need login (no session)
    db_session.add(Account(phone="+821010000003", status="active", session_data=None, tenant_id="t-1"))
    # Banned
    db_session.add(Account(phone="+821010000004", status="banned", tenant_id="t-1"))

    db_session.add(Tenant(id="t-1", phone="+821010000001", plan="pro", subscription_status="active"))
    db_session.add(Tenant(id="t-2", phone="+821010000002", plan="free", subscription_status="inactive"))

    # A completed payment today (amount_usdt cents)
    db_session.add(PaymentRecord(
        tx_id="tx-1",
        tenant_id="t-1",
        from_address="addr",
        amount_usdt=3000,  # $30
        plan="pro",
        status="completed",
    ))
    await db_session.commit()


async def test_overview_requires_admin(unauthenticated_client):
    res = await unauthenticated_client.get("/api/admin/operations/overview")
    assert res.status_code == 401


async def test_overview_returns_all_sections(unauthenticated_client, db_session):
    app = await _wire_db(unauthenticated_client, db_session)
    await _seed_data(db_session)
    try:
        res = await unauthenticated_client.get("/api/admin/operations/overview", headers=_admin_headers())
        assert res.status_code == 200, res.text
        body = res.json()

        assert "accounts" in body
        assert "revenue" in body
        assert "health" in body
        assert "scheduler" in body
        assert "redis" in body
        assert "stats" in body
        assert "dashboard_status" in body
        assert "active_subscriptions" in body
        assert "generated_at" in body
    finally:
        _unwire_db(app)


async def test_overview_account_buckets(unauthenticated_client, db_session):
    app = await _wire_db(unauthenticated_client, db_session)
    await _seed_data(db_session)
    try:
        res = await unauthenticated_client.get("/api/admin/operations/overview", headers=_admin_headers())
        body = res.json()
        acc = body["accounts"]
        assert acc["total"] == 4
        assert acc["connected"] == 1
        assert acc["flood_wait"] == 1
        assert acc["need_login"] == 1
        assert acc["banned"] == 1
    finally:
        _unwire_db(app)


async def test_overview_revenue_today(unauthenticated_client, db_session):
    app = await _wire_db(unauthenticated_client, db_session)
    await _seed_data(db_session)
    try:
        res = await unauthenticated_client.get("/api/admin/operations/overview", headers=_admin_headers())
        body = res.json()
        assert body["revenue"]["today"] == 30.0
        assert body["revenue"]["monthly"] == 30.0
        assert len(body["revenue"]["trend_30d"]) == 30
        assert body["active_subscriptions"] == 1
    finally:
        _unwire_db(app)
