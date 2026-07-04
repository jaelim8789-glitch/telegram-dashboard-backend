import pytest

from app.config import settings


async def _admin_headers(client) -> dict[str, str]:
    login = await client.post(
        "/api/admin/login", json={"username": settings.admin_username, "password": settings.admin_password}
    )
    token = login.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


async def _register_user(client, phone: str, monkeypatch) -> str:
    captured: dict[str, str] = {}

    async def fake_send(p: str, code: str) -> None:
        captured["code"] = code

    monkeypatch.setattr("app.api.auth.send_verification_sms", fake_send)
    await client.post("/api/auth/send-code", json={"phone": phone})
    verify_res = await client.post("/api/auth/verify-code", json={"phone": phone, "code": captured["code"]})
    return verify_res.json()["api_key"]


@pytest.mark.asyncio
async def test_user_management_endpoints_require_admin(unauthenticated_client):
    assert (await unauthenticated_client.get("/api/admin/users")).status_code == 401
    assert (await unauthenticated_client.post("/api/admin/users/some-id/toggle", json={"is_active": False})).status_code == 401
    assert (await unauthenticated_client.post("/api/admin/users/some-id/reissue-key")).status_code == 401


@pytest.mark.asyncio
async def test_list_users_shows_registered_user(unauthenticated_client, monkeypatch):
    await _register_user(unauthenticated_client, "+821099990001", monkeypatch)
    headers = await _admin_headers(unauthenticated_client)

    res = await unauthenticated_client.get("/api/admin/users", headers=headers)
    assert res.status_code == 200
    phones = [u["phone"] for u in res.json()]
    assert "+821099990001" in phones


@pytest.mark.asyncio
async def test_toggle_user_deactivates_and_reactivates(unauthenticated_client, monkeypatch):
    await _register_user(unauthenticated_client, "+821099990002", monkeypatch)
    headers = await _admin_headers(unauthenticated_client)
    user_id = (await unauthenticated_client.get("/api/admin/users", headers=headers)).json()[0]["id"]

    off_res = await unauthenticated_client.post(
        f"/api/admin/users/{user_id}/toggle", json={"is_active": False}, headers=headers
    )
    assert off_res.status_code == 200
    assert off_res.json()["is_active"] is False

    on_res = await unauthenticated_client.post(
        f"/api/admin/users/{user_id}/toggle", json={"is_active": True}, headers=headers
    )
    assert on_res.json()["is_active"] is True


@pytest.mark.asyncio
async def test_toggle_nonexistent_user_returns_404(unauthenticated_client):
    headers = await _admin_headers(unauthenticated_client)
    res = await unauthenticated_client.post(
        "/api/admin/users/does-not-exist/toggle", json={"is_active": False}, headers=headers
    )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_reissue_key_invalidates_old_key_and_issues_new_one(unauthenticated_client, monkeypatch):
    old_key = await _register_user(unauthenticated_client, "+821099990003", monkeypatch)
    headers = await _admin_headers(unauthenticated_client)
    user_id = (await unauthenticated_client.get("/api/admin/users", headers=headers)).json()[0]["id"]

    reissue_res = await unauthenticated_client.post(f"/api/admin/users/{user_id}/reissue-key", headers=headers)
    assert reissue_res.status_code == 200
    new_key = reissue_res.json()["api_key"]
    assert new_key != old_key
    assert new_key.startswith("sk-")

    old_login = await unauthenticated_client.post("/api/auth/login-with-api-key", json={"api_key": old_key})
    assert old_login.status_code == 401

    new_login = await unauthenticated_client.post("/api/auth/login-with-api-key", json={"api_key": new_key})
    assert new_login.status_code == 200
