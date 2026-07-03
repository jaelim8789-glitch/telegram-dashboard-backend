import pytest

from app.config import settings


@pytest.mark.asyncio
async def test_login_success_returns_token(unauthenticated_client):
    res = await unauthenticated_client.post(
        "/api/admin/login", json={"username": settings.admin_username, "password": settings.admin_password}
    )
    assert res.status_code == 200
    body = res.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]


@pytest.mark.asyncio
async def test_login_wrong_password_returns_401(unauthenticated_client):
    res = await unauthenticated_client.post(
        "/api/admin/login", json={"username": settings.admin_username, "password": "wrong"}
    )
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_login_wrong_password_with_non_ascii_returns_401_not_500(unauthenticated_client):
    # secrets.compare_digest raises TypeError on non-ASCII str input unless both sides
    # are encoded to bytes first — a wrong password containing e.g. Korean characters
    # must still cleanly 401, not 500.
    res = await unauthenticated_client.post(
        "/api/admin/login", json={"username": settings.admin_username, "password": "완전히-틀린-비밀번호"}
    )
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_login_wrong_username_returns_401(unauthenticated_client):
    res = await unauthenticated_client.post(
        "/api/admin/login", json={"username": "someone-else", "password": settings.admin_password}
    )
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_me_requires_token(unauthenticated_client):
    res = await unauthenticated_client.get("/api/admin/me")
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_me_with_valid_token(unauthenticated_client):
    login = await unauthenticated_client.post(
        "/api/admin/login", json={"username": settings.admin_username, "password": settings.admin_password}
    )
    token = login.json()["access_token"]

    res = await unauthenticated_client.get("/api/admin/me", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    assert res.json()["username"] == settings.admin_username


@pytest.mark.asyncio
async def test_me_with_garbage_token_returns_401(unauthenticated_client):
    res = await unauthenticated_client.get("/api/admin/me", headers={"Authorization": "Bearer not-a-real-token"})
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_main_api_rejects_request_without_credentials(unauthenticated_client):
    res = await unauthenticated_client.get("/api/accounts")
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_main_api_accepts_admin_session_token(unauthenticated_client):
    login = await unauthenticated_client.post(
        "/api/admin/login", json={"username": settings.admin_username, "password": settings.admin_password}
    )
    token = login.json()["access_token"]

    res = await unauthenticated_client.get("/api/accounts", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200


@pytest.mark.asyncio
async def test_main_api_rejects_invalid_api_key(unauthenticated_client):
    res = await unauthenticated_client.get("/api/accounts", headers={"X-API-Key": "sk-not-a-real-key"})
    assert res.status_code == 401
