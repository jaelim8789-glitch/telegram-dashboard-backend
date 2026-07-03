import pytest

from app.config import settings


async def _admin_headers(client) -> dict[str, str]:
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
