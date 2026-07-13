import pytest

from app.config import settings


async def _admin_headers(client) -> dict[str, str]:
    from app.core.rate_limiter import reset_rate_limits
    reset_rate_limits()
    login = await client.post(
        "/api/admin/login", json={"username": settings.admin_username, "password": settings.admin_password}
    )
    token = login.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_api_key_endpoints_require_admin(unauthenticated_client):
    assert (await unauthenticated_client.post("/api/admin/api-keys", json={"name": "x"})).status_code == 401
    assert (await unauthenticated_client.get("/api/admin/api-keys")).status_code == 401
    assert (await unauthenticated_client.delete("/api/admin/api-keys/some-id")).status_code == 401


@pytest.mark.asyncio
async def test_create_api_key_returns_full_key_once(unauthenticated_client):
    headers = await _admin_headers(unauthenticated_client)

    res = await unauthenticated_client.post("/api/admin/api-keys", json={"name": "테스트 키"}, headers=headers)
    assert res.status_code == 201
    body = res.json()
    assert body["key"].startswith("sk-")
    assert len(body["key"]) == 35  # "sk-" + 32 hex chars
    assert body["name"] == "테스트 키"


@pytest.mark.asyncio
async def test_list_api_keys_masks_the_key(unauthenticated_client):
    headers = await _admin_headers(unauthenticated_client)
    created = await unauthenticated_client.post("/api/admin/api-keys", json={"name": "목록 테스트"}, headers=headers)
    full_key = created.json()["key"]

    res = await unauthenticated_client.get("/api/admin/api-keys", headers=headers)
    assert res.status_code == 200
    listed = res.json()[0]
    assert listed["masked_key"] != full_key
    assert full_key not in listed["masked_key"]
    assert listed["masked_key"].startswith("sk-")
    assert listed["is_active"] is True
    assert listed["last_used"] is None


@pytest.mark.asyncio
async def test_created_api_key_authenticates_main_api(unauthenticated_client):
    headers = await _admin_headers(unauthenticated_client)
    created = await unauthenticated_client.post("/api/admin/api-keys", json={"name": "동작 확인"}, headers=headers)
    full_key = created.json()["key"]

    res = await unauthenticated_client.get("/api/accounts", headers={"X-API-Key": full_key})
    assert res.status_code == 200

    # last_used should now be set
    listing = await unauthenticated_client.get("/api/admin/api-keys", headers=headers)
    assert listing.json()[0]["last_used"] is not None


@pytest.mark.asyncio
async def test_delete_api_key_revokes_access(unauthenticated_client):
    headers = await _admin_headers(unauthenticated_client)
    created = await unauthenticated_client.post("/api/admin/api-keys", json={"name": "삭제 테스트"}, headers=headers)
    key_id = created.json()["id"]
    full_key = created.json()["key"]

    delete_res = await unauthenticated_client.delete(f"/api/admin/api-keys/{key_id}", headers=headers)
    assert delete_res.status_code == 204

    res = await unauthenticated_client.get("/api/accounts", headers={"X-API-Key": full_key})
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_delete_nonexistent_api_key_returns_404(unauthenticated_client):
    headers = await _admin_headers(unauthenticated_client)
    res = await unauthenticated_client.delete("/api/admin/api-keys/does-not-exist", headers=headers)
    assert res.status_code == 404


# ── Regression: admin-issued API key must authenticate via login-with-api-key ──

@pytest.mark.asyncio
async def test_admin_issued_api_key_can_login_with_api_key(unauthenticated_client, db_session):
    """Regression test for the production API key inconsistency bug.

    API keys created in the Admin "API 키 관리" page are stored in the APIKey
    model (table api_keys), but /auth/login-with-api-key only checks
    User.api_key_hash.  The fix bridges the two systems by also storing the
    key's hash in User.api_key_hash when the admin creates a key with a
    tenant_id.

    This test proves: issue key → login-with-api-key succeeds.
    """
    from app.core.security import hash_api_key
    from app.crud import user as user_crud
    from app.models.tenant import Tenant
    from app.models.user import User
    from sqlalchemy import select

    # 1. Create a user + tenant in the test DB
    phone = "+821099991100"
    user = User(phone=phone)
    db_session.add(user)
    await db_session.flush()
    tenant = Tenant(phone=phone, plan="free", subscription_status="active")
    db_session.add(tenant)
    await db_session.flush()
    await db_session.commit()
    await db_session.refresh(user)

    # 2. Wire the test DB session to the endpoint
    from app.main import app
    import app.database as db_mod
    from app.api.admin import get_db as admin_get_db

    async def _override():
        yield db_session

    app.dependency_overrides[db_mod.get_db] = _override
    app.dependency_overrides[admin_get_db] = _override

    try:
        # 3. Admin creates an API key for this tenant
        headers = await _admin_headers(unauthenticated_client)
        create_res = await unauthenticated_client.post(
            "/api/admin/api-keys",
            json={"name": "regression-test", "tenant_id": tenant.id},
            headers=headers,
        )
        assert create_res.status_code == 201
        raw_key = create_res.json()["key"]

        # 4. Verify the key's hash was stored in User.api_key_hash
        updated_user = await user_crud.get_user_by_phone(db_session, phone)
        assert updated_user is not None
        assert updated_user.api_key_hash == hash_api_key(raw_key), (
            "User.api_key_hash must be set so login-with-api-key can authenticate"
        )

        # 5. Login with the key via login-with-api-key
        login_res = await unauthenticated_client.post(
            "/api/auth/login-with-api-key",
            json={"api_key": raw_key},
        )
        assert login_res.status_code == 200, (
            f"Admin-issued API key must authenticate via login-with-api-key. "
            f"Got {login_res.status_code}: {login_res.text}"
        )
        body = login_res.json()
        assert body["token_type"] == "bearer"
        assert body["access_token"]
    finally:
        app.dependency_overrides.pop(db_mod.get_db, None)
        app.dependency_overrides.pop(admin_get_db, None)