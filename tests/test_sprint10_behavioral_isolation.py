"""
Sprint 10: Behavioral Tenant A/B Isolation Tests.
Creates real Tenant A and Tenant B resources and proves cross-tenant
isolation at the HTTP level using mocked identity contexts.
"""

import asyncio
import pytest
from contextlib import contextmanager
from fastapi.testclient import TestClient

from app.main import app
from app.api.deps import get_current_identity, Identity


# ─── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def client():
    """TestClient with no dependency overrides (real auth resolution)."""
    return TestClient(app)


@pytest.fixture
def tenant_a():
    """Simulate Tenant A identity via dependency override."""
    identity = Identity(kind="user", tenant_id="tenant-A")
    app.dependency_overrides[get_current_identity] = lambda: identity
    yield
    app.dependency_overrides.pop(get_current_identity, None)


@pytest.fixture
def tenant_b():
    """Simulate Tenant B identity."""
    identity = Identity(kind="user", tenant_id="tenant-B")
    app.dependency_overrides[get_current_identity] = lambda: identity
    yield
    app.dependency_overrides.pop(get_current_identity, None)


@pytest.fixture
def api_key_tenant_a():
    """Simulate API key with tenant_id=A."""
    identity = Identity(kind="api_key", tenant_id="tenant-A")
    app.dependency_overrides[get_current_identity] = lambda: identity
    yield
    app.dependency_overrides.pop(get_current_identity, None)


@pytest.fixture
def api_key_no_tenant():
    """Simulate API key without tenant context."""
    identity = Identity(kind="api_key", tenant_id=None)
    app.dependency_overrides[get_current_identity] = lambda: identity
    yield
    app.dependency_overrides.pop(get_current_identity, None)


def _setup_tenants_and_accounts(*, tenant_a_id, tenant_b_id):
    """Helper: create two tenants and an account for each. Returns (acc_a_id, acc_b_id)."""
    from app.database import async_session_maker
    from app.models.account import Account
    from app.models.tenant import Tenant

    async def _setup():
        async with async_session_maker() as db:
            ta = Tenant(id=tenant_a_id, name="Tenant A", phone=f"+8200000{tenant_a_id[-4:]}")
            tb = Tenant(id=tenant_b_id, name="Tenant B", phone=f"+8200000{tenant_b_id[-4:]}")
            db.add_all([ta, tb])
            await db.commit()

            acc_a = Account(
                phone=f"+8210000{tenant_a_id[-4:]}1", tenant_id=tenant_a_id, name="A-Account"
            )
            acc_b = Account(
                phone=f"+8210000{tenant_b_id[-4:]}2", tenant_id=tenant_b_id, name="B-Account"
            )
            db.add_all([acc_a, acc_b])
            await db.commit()
            return acc_a.id, acc_b.id

    return asyncio.run(_setup())


def _cleanup(acc_ids, tenant_ids):
    """Cleanup test tenants and accounts."""
    from app.database import async_session_maker
    from app.models.account import Account
    from app.models.tenant import Tenant

    async def _clean():
        async with async_session_maker() as db:
            for aid in acc_ids:
                a = await db.get(Account, aid)
                if a:
                    await db.delete(a)
            for tid in tenant_ids:
                t = await db.get(Tenant, tid)
                if t:
                    await db.delete(t)
            await db.commit()

    asyncio.run(_clean())


# ═══════════════════════════════════════════════════════════════════════
# Phase 1: Account LIST isolation — Tenant A/B behavioral
# ═══════════════════════════════════════════════════════════════════════

def test_tenant_a_cannot_list_tenant_b_accounts(client, tenant_a):
    """Tenant A LIST /api/accounts must NOT return Tenant B accounts."""
    acc_a_id, acc_b_id = _setup_tenants_and_accounts(
        tenant_a_id="tenant-A-beh", tenant_b_id="tenant-B-beh"
    )

    try:
        resp = client.get("/api/accounts")
        assert resp.status_code == 200
        ids = [a["id"] for a in resp.json()]
        assert acc_a_id in ids, "Tenant A must see its own account"
        assert acc_b_id not in ids, "Tenant A must NOT see Tenant B account"
    finally:
        _cleanup([acc_a_id, acc_b_id], ["tenant-A-beh", "tenant-B-beh"])


def test_tenant_a_cannot_get_tenant_b_account_by_id(client, tenant_a):
    """Tenant A GET /api/accounts/{b_id} must return 403/404."""
    _, acc_b_id = _setup_tenants_and_accounts(
        tenant_a_id="tenant-A-get", tenant_b_id="tenant-B-get"
    )

    try:
        resp = client.get(f"/api/accounts/{acc_b_id}")
        assert resp.status_code in (403, 404), (
            f"Tenant A must be denied access to Tenant B account, got {resp.status_code}"
        )
    finally:
        _cleanup([acc_b_id], ["tenant-A-get", "tenant-B-get"])


def test_tenant_a_cannot_update_tenant_b_account(client, tenant_a):
    """Tenant A PUT /api/accounts/{b_id} must return 403/404."""
    _, acc_b_id = _setup_tenants_and_accounts(
        tenant_a_id="tenant-A-upd", tenant_b_id="tenant-B-upd"
    )

    try:
        resp = client.put(f"/api/accounts/{acc_b_id}", json={"name": "Hacked"})
        assert resp.status_code in (403, 404), (
            f"Tenant A must be denied update of Tenant B account, got {resp.status_code}"
        )
    finally:
        _cleanup([acc_b_id], ["tenant-A-upd", "tenant-B-upd"])


def test_tenant_a_cannot_delete_tenant_b_account(client, tenant_a):
    """Tenant A DELETE /api/accounts/{b_id} must return 403/404."""
    _, acc_b_id = _setup_tenants_and_accounts(
        tenant_a_id="tenant-A-del", tenant_b_id="tenant-B-del"
    )

    try:
        resp = client.delete(f"/api/accounts/{acc_b_id}")
        assert resp.status_code in (403, 404), (
            f"Tenant A must be denied delete of Tenant B account, got {resp.status_code}"
        )
    finally:
        _cleanup([acc_b_id], ["tenant-A-del", "tenant-B-del"])


# ═══════════════════════════════════════════════════════════════════════
# Phase 2: Telegram Auth operation isolation
# ═══════════════════════════════════════════════════════════════════════

def test_tenant_a_cannot_send_code_on_tenant_b_account(client, tenant_a):
    """Tenant A POST /api/accounts/{b_id}/send-code must be denied before Telethon call."""
    _, acc_b_id = _setup_tenants_and_accounts(
        tenant_a_id="tenant-A-sc", tenant_b_id="tenant-B-sc"
    )

    try:
        resp = client.post(f"/api/accounts/{acc_b_id}/send-code")
        assert resp.status_code in (403, 404), (
            f"Tenant A must be denied send-code on Tenant B account, got {resp.status_code}"
        )
    finally:
        _cleanup([acc_b_id], ["tenant-A-sc", "tenant-B-sc"])


def test_tenant_a_cannot_verify_code_on_tenant_b_account(client, tenant_a):
    """Tenant A POST /api/accounts/{b_id}/verify-code must be denied."""
    _, acc_b_id = _setup_tenants_and_accounts(
        tenant_a_id="tenant-A-vc", tenant_b_id="tenant-B-vc"
    )

    try:
        resp = client.post(f"/api/accounts/{acc_b_id}/verify-code", json={"code": "12345"})
        assert resp.status_code in (403, 404), (
            f"Tenant A must be denied verify-code on Tenant B account, got {resp.status_code}"
        )
    finally:
        _cleanup([acc_b_id], ["tenant-A-vc", "tenant-B-vc"])


def test_tenant_a_cannot_verify_2fa_on_tenant_b_account(client, tenant_a):
    """Tenant A POST /api/accounts/{b_id}/verify-2fa must be denied."""
    _, acc_b_id = _setup_tenants_and_accounts(
        tenant_a_id="tenant-A-2fa", tenant_b_id="tenant-B-2fa"
    )

    try:
        resp = client.post(f"/api/accounts/{acc_b_id}/verify-2fa", json={"password": "hunter2"})
        assert resp.status_code in (403, 404), (
            f"Tenant A must be denied verify-2fa on Tenant B account, got {resp.status_code}"
        )
    finally:
        _cleanup([acc_b_id], ["tenant-A-2fa", "tenant-B-2fa"])


def test_tenant_a_cannot_get_status_on_tenant_b_account(client, tenant_a):
    """Tenant A GET /api/accounts/{b_id}/status must be denied."""
    _, acc_b_id = _setup_tenants_and_accounts(
        tenant_a_id="tenant-A-st", tenant_b_id="tenant-B-st"
    )

    try:
        resp = client.get(f"/api/accounts/{acc_b_id}/status")
        assert resp.status_code in (403, 404), (
            f"Tenant A must be denied status check on Tenant B account, got {resp.status_code}"
        )
    finally:
        _cleanup([acc_b_id], ["tenant-A-st", "tenant-B-st"])


# ═══════════════════════════════════════════════════════════════════════
# Phase 3: require_tenant_access unit tests
# ═══════════════════════════════════════════════════════════════════════

def test_require_tenant_access_admin_bypass():
    """Admin identity must bypass require_tenant_access."""
    from app.api.deps import require_tenant_access, Identity

    async def test_admin():
        identity = Identity(kind="admin")
        await require_tenant_access("any-tenant", identity)
        return True

    assert asyncio.run(test_admin()), "Admin must bypass tenant access check"


def test_require_tenant_access_missing_context():
    """Identity with tenant_id=None must be rejected."""
    from app.api.deps import require_tenant_access, Identity
    from fastapi import HTTPException

    async def test_api_key():
        identity = Identity(kind="api_key", tenant_id=None)
        try:
            await require_tenant_access("some-tenant", identity)
            return False
        except HTTPException as e:
            return e.status_code == 403

    assert asyncio.run(test_api_key()), "Missing tenant context must raise 403"


def test_require_tenant_access_wrong_tenant():
    """Identity with tenant_id=A must not access tenant B resources."""
    from app.api.deps import require_tenant_access, Identity
    from fastapi import HTTPException

    async def test_wrong():
        identity = Identity(kind="user", tenant_id="tenant-A")
        try:
            await require_tenant_access("tenant-B", identity)
            return False
        except HTTPException as e:
            return e.status_code == 403

    assert asyncio.run(test_wrong()), "Cross-tenant access must raise 403"


def test_require_tenant_access_correct_tenant():
    """Identity with tenant_id=A must access tenant A resources."""
    from app.api.deps import require_tenant_access, Identity

    async def test_correct():
        identity = Identity(kind="user", tenant_id="tenant-A")
        await require_tenant_access("tenant-A", identity)
        return True

    assert asyncio.run(test_correct()), "Same-tenant access must succeed"


# ═══════════════════════════════════════════════════════════════════════
# Phase 4: API-key tenant policy
# ═══════════════════════════════════════════════════════════════════════

def test_api_key_with_wrong_tenant_gets_403(client, api_key_tenant_a):
    """API key with tenant_id=A must get 403 for tenant B resources."""
    resp = client.get("/api/features/tenant-B/templates")
    assert resp.status_code == 403, (
        f"API key with tenant A must get 403 for tenant B, got {resp.status_code}"
    )


def test_api_key_without_tenant_context_gets_403(client, api_key_no_tenant):
    """API key with tenant_id=None must get 403 for tenant-scoped resources."""
    resp = client.get("/api/features/some-tenant/templates")
    assert resp.status_code == 403, (
        f"API key without tenant context must get 403, got {resp.status_code}"
    )


# ═══════════════════════════════════════════════════════════════════════
# Phase 5: NULL-tenant Account policy
# ═══════════════════════════════════════════════════════════════════════

def test_null_tenant_account_not_listed_for_tenant_user(client, tenant_a):
    """Accounts with tenant_id=NULL must NOT appear in a tenant user's LIST."""
    from app.database import async_session_maker
    from app.models.account import Account
    from app.models.tenant import Tenant

    async def _setup():
        async with async_session_maker() as db:
            ta = Tenant(id="tenant-A-null", name="Tenant A", phone="+82000000017")
            db.add(ta)
            await db.commit()
            acc_null = Account(phone="+82000000100", tenant_id=None, name="Null-Tenant-Account")
            acc_a = Account(phone="+82000000101", tenant_id="tenant-A-null", name="A-Account")
            db.add_all([acc_null, acc_a])
            await db.commit()
            return acc_null.id, acc_a.id

    acc_null_id, acc_a_id = asyncio.run(_setup())

    try:
        # Override identity to tenant-A
        from app.api.deps import get_current_identity, Identity
        app.dependency_overrides[get_current_identity] = lambda: Identity(kind="user", tenant_id="tenant-A-null")

        resp = client.get("/api/accounts")
        assert resp.status_code == 200
        ids = [a["id"] for a in resp.json()]
        assert acc_a_id in ids, "Tenant A must see its own account"
        assert acc_null_id not in ids, "NULL-tenant account must NOT appear in tenant A's list"
    finally:
        app.dependency_overrides.pop(get_current_identity, None)
        # Cleanup
        async def _clean():
            async with async_session_maker() as db:
                for aid in [acc_null_id, acc_a_id]:
                    a = await db.get(Account, aid)
                    if a: await db.delete(a)
                t = await db.get(Tenant, "tenant-A-null")
                if t: await db.delete(t)
                await db.commit()
        asyncio.run(_clean())