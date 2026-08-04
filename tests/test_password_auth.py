import pytest


@pytest.mark.asyncio
async def test_register_password_then_login_succeeds(unauthenticated_client):
    reg = await unauthenticated_client.post(
        "/api/auth/register-password", json={"username": "guest_alice", "password": "correcthorse123"}
    )
    assert reg.status_code == 201
    body = reg.json()
    assert body["access_token"]
    assert body["session_token"]

    login = await unauthenticated_client.post(
        "/api/auth/login-password", json={"username": "guest_alice", "password": "correcthorse123"}
    )
    assert login.status_code == 200
    assert login.json()["session_token"]


@pytest.mark.asyncio
async def test_register_password_grants_free_plan_via_me(unauthenticated_client):
    reg = await unauthenticated_client.post(
        "/api/auth/register-password", json={"username": "guest_bob", "password": "correcthorse123"}
    )
    session_token = reg.json()["session_token"]

    me = await unauthenticated_client.get("/api/auth/me", headers={"X-Session-Token": session_token})
    assert me.status_code == 200
    body = me.json()
    assert body["plan"] == "free"
    assert body["subscription_status"] == "active"


@pytest.mark.asyncio
async def test_register_password_duplicate_username_rejected(unauthenticated_client):
    await unauthenticated_client.post(
        "/api/auth/register-password", json={"username": "guest_carol", "password": "correcthorse123"}
    )
    dup = await unauthenticated_client.post(
        "/api/auth/register-password", json={"username": "guest_carol", "password": "anotherpassword1"}
    )
    assert dup.status_code == 409


@pytest.mark.asyncio
async def test_register_password_rejects_short_password(unauthenticated_client):
    res = await unauthenticated_client.post(
        "/api/auth/register-password", json={"username": "guest_dave", "password": "short1"}
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_register_password_rejects_invalid_username(unauthenticated_client):
    res = await unauthenticated_client.post(
        "/api/auth/register-password", json={"username": "a b!", "password": "correcthorse123"}
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_login_password_wrong_password_rejected(unauthenticated_client):
    await unauthenticated_client.post(
        "/api/auth/register-password", json={"username": "guest_erin", "password": "correcthorse123"}
    )
    res = await unauthenticated_client.post(
        "/api/auth/login-password", json={"username": "guest_erin", "password": "wrongpassword1"}
    )
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_login_password_unknown_username_rejected(unauthenticated_client):
    res = await unauthenticated_client.post(
        "/api/auth/login-password", json={"username": "nonexistent_user_xyz", "password": "whatever123"}
    )
    assert res.status_code == 401
