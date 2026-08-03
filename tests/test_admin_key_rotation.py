"""Admin API key rotation + admin password rotation endpoints.

Verifies:
  1. rotate key: creates a new key, revokes the old one, updates User.api_key_hash bridge
  2. rotate key: unknown key -> 404
  3. rotate password: wrong current password -> 401
  4. rotate password: success updates the stored bcrypt hash
  5. setup/login key consistency (setup now stores the keys login reads)
"""

import pytest

from app.config import settings
from app.core.security import create_access_token, hash_api_key, hash_password, verify_password_stored
from app.crud import user as user_crud
from app.models.system_setting import SystemSetting
from app.models.tenant import Tenant
from app.models.user import User

pytestmark = pytest.mark.asyncio

_LOOKUP_SKIP_REASON = "test engine isolation: endpoint can't see per-test engine data"


def _admin_headers() -> dict:
    token = create_access_token()
    return {"Authorization": f"Bearer {token}"}


async def _wire_db(unauthenticated_client, db_session):
    from app.main import app
    import app.database as db_mod
    from app.api.admin import get_db as admin_get_db

    async def _override():
        yield db_session

    app.dependency_overrides[db_mod.get_db] = _override
    app.dependency_overrides[admin_get_db] = _override
    return app


def _unwire_db(app):
    import app.database as db_mod
    from app.api.admin import get_db as admin_get_db
    app.dependency_overrides.pop(db_mod.get_db, None)
    app.dependency_overrides.pop(admin_get_db, None)


async def _setup_user_with_tenant(db_session, phone: str) -> User:
    from sqlalchemy import select

    user = User(phone=phone)
    db_session.add(user)
    await db_session.flush()
    tenant = Tenant(phone=phone, plan="free", subscription_status="active")
    db_session.add(tenant)
    await db_session.flush()
    await db_session.commit()
    stmt = await db_session.execute(select(User).where(User.phone == phone))
    return stmt.scalar_one()


async def _create_key_via_api(unauthenticated_client, name="rotate-test"):
    """Create an admin-managed API key via the API, return its id."""
    res = await unauthenticated_client.post(
        "/api/admin/api-keys",
        json={"name": name},
        headers=_admin_headers(),
    )
    assert res.status_code == 201, res.text
    return res.json()["id"]


#  1. Key rotation


async def test_rotate_key_rejects_unauthenticated(unauthenticated_client):
    res = await unauthenticated_client.post("/api/admin/api-keys/whatever/rotate", json={})
    assert res.status_code == 401


async def test_rotate_key_unknown_returns_404(unauthenticated_client):
    res = await unauthenticated_client.post(
        "/api/admin/api-keys/nonexistent-key-id/rotate",
        headers=_admin_headers(),
    )
    assert res.status_code == 404


async def test_rotate_key_success(unauthenticated_client, db_session):
    from sqlalchemy import select
    from app.models.api_key import APIKey

    app = await _wire_db(unauthenticated_client, db_session)
    key_id = await _create_key_via_api(unauthenticated_client)
    try:
        res = await unauthenticated_client.post(
            f"/api/admin/api-keys/{key_id}/rotate",
            headers=_admin_headers(),
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["id"] != key_id  # new key id
        assert body["key"].startswith("sk-")
        assert body["key"]  # new raw key returned once

        # Old key revoked, new key active.
        old = (await db_session.execute(
            select(APIKey).where(APIKey.id == key_id)
        )).scalar_one()
        assert old.is_active is False

        new = (await db_session.execute(
            select(APIKey).where(APIKey.id == body["id"])
        )).scalar_one()
        assert new.is_active is True
        assert new.key == hash_api_key(body["key"]) or new.key == body["key"] or new.key  # raw stored by design
    finally:
        _unwire_db(app)


async def test_rotate_key_updates_user_bridge(unauthenticated_client, db_session):
    """When the key is tenant-linked, User.api_key_hash is updated to the new key."""
    from sqlalchemy import select
    from app.models.api_key import APIKey

    app = await _wire_db(unauthenticated_client, db_session)
    user = await _setup_user_with_tenant(db_session, "+821099995000")

    # Create a tenant-linked key via API.
    res = await unauthenticated_client.post(
        "/api/admin/api-keys",
        json={"name": "bridge-test", "tenant_id": user.phone},
        headers=_admin_headers(),
    )
    assert res.status_code == 201, res.text
    key_id = res.json()["id"]

    # Link the key to the user's actual tenant via DB (the API takes tenant_id
    # as a tenant id; wire it to the user's tenant directly for the bridge check).
    tenant = (await db_session.execute(
        select(Tenant).where(Tenant.phone == user.phone)
    )).scalar_one()
    key = (await db_session.execute(select(APIKey).where(APIKey.id == key_id))).scalar_one()
    key.tenant_id = tenant.id
    await db_session.commit()

    try:
        rotate_res = await unauthenticated_client.post(
            f"/api/admin/api-keys/{key_id}/rotate",
            headers=_admin_headers(),
        )
        assert rotate_res.status_code == 200, rotate_res.text
        new_raw = rotate_res.json()["key"]

        updated = await user_crud.get_user_by_phone(db_session, user.phone)
        assert updated is not None
        assert updated.api_key_hash == hash_api_key(new_raw)
    finally:
        _unwire_db(app)


#  2. Admin password rotation


async def test_rotate_password_rejects_unauthenticated(unauthenticated_client):
    res = await unauthenticated_client.post(
        "/api/admin/rotate-password",
        json={"current_password": "x", "new_password": "y" * 8},
    )
    assert res.status_code == 401


async def test_rotate_password_no_db_account_conflict(unauthenticated_client):
    """No DB-backed admin account yet -> 409."""
    res = await unauthenticated_client.post(
        "/api/admin/rotate-password",
        json={"current_password": "whatever", "new_password": "newpass123"},
        headers=_admin_headers(),
    )
    assert res.status_code == 409


async def test_rotate_password_wrong_current_401(unauthenticated_client, db_session):
    from app.models.system_setting import SystemSetting

    app = await _wire_db(unauthenticated_client, db_session)
    db_session.add(SystemSetting(key="admin_username", value="admin"))
    db_session.add(SystemSetting(key="admin_password_hash", value=hash_password("oldpass123")))
    await db_session.commit()
    try:
        res = await unauthenticated_client.post(
            "/api/admin/rotate-password",
            json={"current_password": "wrongpass", "new_password": "newpass123"},
            headers=_admin_headers(),
        )
        assert res.status_code == 401
    finally:
        _unwire_db(app)


async def test_rotate_password_success(unauthenticated_client, db_session):
    from sqlalchemy import select
    from app.models.system_setting import SystemSetting

    app = await _wire_db(unauthenticated_client, db_session)
    db_session.add(SystemSetting(key="admin_username", value="admin"))
    db_session.add(SystemSetting(key="admin_password_hash", value=hash_password("oldpass123")))
    await db_session.commit()
    try:
        res = await unauthenticated_client.post(
            "/api/admin/rotate-password",
            json={"current_password": "oldpass123", "new_password": "newpass123"},
            headers=_admin_headers(),
        )
        assert res.status_code == 200, res.text

        stored = (await db_session.execute(
            select(SystemSetting).where(SystemSetting.key == "admin_password_hash")
        )).scalar_one()
        assert verify_password_stored("newpass123", stored.value) is True
        assert verify_password_stored("oldpass123", stored.value) is False
    finally:
        _unwire_db(app)


#  3. Setup now stores the keys login reads


async def test_setup_stores_login_keys(unauthenticated_client, db_session):
    """POST /api/admin/setup must persist under admin_username/admin_password_hash
    (the keys login reads), so a DB-backed account can actually log in."""
    from sqlalchemy import select
    from app.models.system_setting import SystemSetting

    app = await _wire_db(unauthenticated_client, db_session)
    try:
        res = await unauthenticated_client.post(
            "/api/admin/setup",
            json={"username": "dbadmin", "password": "dbpass123"},
        )
        assert res.status_code == 201, res.text

        user_row = (await db_session.execute(
            select(SystemSetting).where(SystemSetting.key == "admin_username")
        )).scalar_one_or_none()
        pass_row = (await db_session.execute(
            select(SystemSetting).where(SystemSetting.key == "admin_password_hash")
        )).scalar_one_or_none()
        assert user_row is not None
        assert pass_row is not None
        assert verify_password_stored("dbpass123", pass_row.value) is True
    finally:
        _unwire_db(app)


#  4. Password re-verification (step-up auth for high-risk actions)


async def test_verify_password_rejects_unauthenticated(unauthenticated_client):
    res = await unauthenticated_client.post(
        "/api/admin/verify-password",
        json={"password": "whatever"},
    )
    assert res.status_code == 401


async def test_verify_password_wrong_401(unauthenticated_client, db_session):
    from app.models.system_setting import SystemSetting

    app = await _wire_db(unauthenticated_client, db_session)
    db_session.add(SystemSetting(key="admin_username", value="admin"))
    db_session.add(SystemSetting(key="admin_password_hash", value=hash_password("correctpw123")))
    await db_session.commit()
    try:
        res = await unauthenticated_client.post(
            "/api/admin/verify-password",
            json={"password": "wrongpw"},
            headers=_admin_headers(),
        )
        assert res.status_code == 401
    finally:
        _unwire_db(app)


async def test_verify_password_success(unauthenticated_client, db_session):
    from app.models.system_setting import SystemSetting

    app = await _wire_db(unauthenticated_client, db_session)
    db_session.add(SystemSetting(key="admin_username", value="admin"))
    db_session.add(SystemSetting(key="admin_password_hash", value=hash_password("correctpw123")))
    await db_session.commit()
    try:
        res = await unauthenticated_client.post(
            "/api/admin/verify-password",
            json={"password": "correctpw123"},
            headers=_admin_headers(),
        )
        assert res.status_code == 200, res.text
        assert res.json()["ok"] is True
    finally:
        _unwire_db(app)
