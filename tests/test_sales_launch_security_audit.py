"""Regression tests for the pre-sales-launch security audit.

Each test class corresponds to one verified, fixed production bug:

- BillingAdminGating: /api/billing/usdt/confirm and /api/billing/stars/add
  unconditionally activated plans / credited Stars from client-supplied,
  unverified input, and were reachable by any authenticated member of the
  tenant (not just admin, despite the confirm endpoint's own docstring).
- PaymentStatusLookup: GET /api/payment/status/{ref} matched PaymentRecord.tx_id
  (a blockchain hash) against the payment_ref (memo) — which never matches —
  then fell back to an unscoped APIKey.name ILIKE '%{plan}%' lookup that could
  return a different tenant's masked API key, or crash once two tenants ever
  shared a plan.
- GroupSearchJoinIsolation: join_selected_groups processed every row returned
  for a client-supplied result_ids list without filtering to the account whose
  tenant access was actually checked, so mixing in another account's result
  ids would use the checked account's Telegram session to join groups it
  never searched for.
"""

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.security import create_access_token
from app.crud import account as account_crud
from app.models.api_key import APIKey
from app.models.tenant import PaymentRecord, Tenant
from app.schemas.account import AccountCreate


def _admin_headers() -> dict:
    return {"Authorization": f"Bearer {create_access_token()}"}


async def _make_tenant(db_session, *, phone, plan="basic", status="pending", payment_ref=None):
    tenant = Tenant(phone=phone, plan=plan, subscription_status=status, payment_ref=payment_ref)
    db_session.add(tenant)
    await db_session.commit()
    await db_session.refresh(tenant)
    return tenant


# ═══════════════════════════════════════════════════════════════════════
# Billing: usdt/confirm and stars/add must require real admin auth
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_usdt_confirm_rejected_without_admin_auth(unauthenticated_client, db_session):
    tenant = await _make_tenant(db_session, phone="+821000010001", plan="enterprise", status="pending")

    res = await unauthenticated_client.post(
        f"/api/billing/usdt/confirm?tenant_id={tenant.id}&tx_hash=not-a-real-tx"
    )
    assert res.status_code in (401, 403)

    await db_session.refresh(tenant)
    assert tenant.subscription_status == "pending"  # never activated by the rejected call


@pytest.mark.asyncio
async def test_usdt_confirm_succeeds_with_real_admin_token(unauthenticated_client, db_session, monkeypatch):
    """Admin gating itself must let a correctly-authenticated request through.

    tx_hash must still be a real, Trongrid-verified transaction (see the H4 fix in
    tests/test_billing_entitlements.py) — mock the chain lookup here so this test
    stays about auth gating, not blockchain verification, and doesn't hit the real
    network.
    """
    import app.services.usdt_watcher as watcher_module

    async def fake_transactions():
        return [{
            "tx_id": "confirmed-tx-hash",
            "from_address": "Txxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            "amount_usdt": 15.0,
            "amount_cents": 1500,
            "block_timestamp": 1234567890,
            "memo": "",
        }]

    monkeypatch.setattr(watcher_module, "get_usdt_transactions", fake_transactions)

    tenant = await _make_tenant(db_session, phone="+821000010002", plan="basic", status="pending")

    res = await unauthenticated_client.post(
        f"/api/billing/usdt/confirm?tenant_id={tenant.id}&tx_hash=confirmed-tx-hash",
        headers=_admin_headers(),
    )
    assert res.status_code == 200
    await db_session.refresh(tenant)
    assert tenant.subscription_status == "active"


@pytest.mark.asyncio
async def test_stars_add_rejected_without_admin_auth(unauthenticated_client, db_session):
    tenant = await _make_tenant(db_session, phone="+821000010003", plan="basic", status="active")

    res = await unauthenticated_client.post(
        f"/api/billing/stars/add?tenant_id={tenant.id}&stars_amount=999999"
    )
    assert res.status_code in (401, 403)

    await db_session.refresh(tenant)
    assert (tenant.stars_balance or 0) == 0  # no free Stars credited by the rejected call


@pytest.mark.asyncio
async def test_stars_add_succeeds_with_real_admin_token(unauthenticated_client, db_session):
    tenant = await _make_tenant(db_session, phone="+821000010004", plan="basic", status="active")

    res = await unauthenticated_client.post(
        f"/api/billing/stars/add?tenant_id={tenant.id}&stars_amount=100",
        headers=_admin_headers(),
    )
    assert res.status_code == 200
    await db_session.refresh(tenant)
    assert tenant.stars_balance == 100


# ═══════════════════════════════════════════════════════════════════════
# Payment status lookup: must be tenant-scoped, never leak another
# tenant's API key, and never crash when plans collide across tenants
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_payment_status_returns_own_tenant_key_not_a_stranger(unauthenticated_client, db_session):
    """Two tenants share the same plan name — checking one's status must never surface
    the other's key (the old APIKey.name ILIKE '%{plan}%' lookup ignored tenant_id
    entirely and could return either one, or crash outright)."""
    victim = await _make_tenant(db_session, phone="+821000020001", plan="basic", status="active", payment_ref="TM-VICTIM1")
    caller = await _make_tenant(db_session, phone="+821000020002", plan="basic", status="active", payment_ref="TM-CALLER1")

    victim_key = APIKey(key="sk-" + "v" * 40, name="USDT-basic-auto", is_active=True)
    caller_key = APIKey(key="sk-" + "c" * 40, name="USDT-basic-auto", is_active=True)
    db_session.add_all([victim_key, caller_key])
    await db_session.commit()
    await db_session.refresh(victim_key)
    await db_session.refresh(caller_key)

    db_session.add_all([
        PaymentRecord(tx_id="chain-tx-victim", tenant_id=victim.id, from_address="a", amount_usdt=1500,
                       plan="basic", status="completed", api_key_id=victim_key.id, block_timestamp=1),
        PaymentRecord(tx_id="chain-tx-caller", tenant_id=caller.id, from_address="b", amount_usdt=1500,
                      plan="basic", status="completed", api_key_id=caller_key.id, block_timestamp=2),
    ])
    await db_session.commit()

    res = await unauthenticated_client.get(f"/api/payment/status/{caller.payment_ref}")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "completed"
    assert body["api_key_masked"] == caller_key.key[:8] + "..." + caller_key.key[-4:]
    assert body["api_key_masked"] != victim_key.key[:8] + "..." + victim_key.key[-4:]


@pytest.mark.asyncio
async def test_payment_status_pending_tenant_not_yet_active(unauthenticated_client, db_session):
    tenant = await _make_tenant(db_session, phone="+821000020003", plan="basic", status="pending", payment_ref="TM-PEND0001")

    res = await unauthenticated_client.get(f"/api/payment/status/{tenant.payment_ref}")
    assert res.status_code == 200
    assert res.json()["status"] == "pending"


@pytest.mark.asyncio
async def test_payment_status_unknown_ref_reports_pending_not_error(unauthenticated_client, db_session):
    res = await unauthenticated_client.get("/api/payment/status/TM-DOESNOTEXIST")
    assert res.status_code == 200
    assert res.json()["status"] == "pending"


# ═══════════════════════════════════════════════════════════════════════
# Group search: joining must never act on another account's search results
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_join_selected_groups_ignores_rows_from_other_accounts(db_session, monkeypatch):
    from app.models.group_search import GroupSearchResult
    from app.services import group_search_service

    account_mine = await account_crud.create_account(db_session, AccountCreate(phone="+821000030001"))
    account_other = await account_crud.create_account(db_session, AccountCreate(phone="+821000030002"))

    my_result = GroupSearchResult(account_id=account_mine.id, keyword="k", chat_id="1", title="mine", username="mine_grp")
    other_result = GroupSearchResult(account_id=account_other.id, keyword="k", chat_id="2", title="other", username="other_grp")
    db_session.add_all([my_result, other_result])
    await db_session.commit()
    await db_session.refresh(my_result)
    await db_session.refresh(other_result)

    class _FakeClient:
        async def get_entity(self, ident):
            return ident

        async def __call__(self, request):
            return None

    async def _fake_get_authorized_client(account):
        return _FakeClient()

    monkeypatch.setattr(group_search_service, "get_authorized_client", _fake_get_authorized_client)
    monkeypatch.setattr(
        group_search_service, "async_session_maker", async_sessionmaker(db_session.bind, expire_on_commit=False)
    )

    results = await group_search_service.join_selected_groups(
        account_mine, [my_result.id, other_result.id]
    )

    processed_titles = {r["title"] for r in results}
    assert my_result.title in processed_titles
    assert other_result.title not in processed_titles
    assert len(results) == 1
