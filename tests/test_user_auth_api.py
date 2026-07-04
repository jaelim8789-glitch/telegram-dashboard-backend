from unittest.mock import AsyncMock

import pytest

from app.core.limits import OTP_MAX_ATTEMPTS
from app.crud import user as user_crud

# The OTP is stored only as a hash, so tests can't recover the code from the DB — each
# test that needs the real code monkeypatches app.api.auth.send_verification_sms to
# capture it instead of actually sending (or logging) it.


@pytest.mark.asyncio
async def test_send_code_then_verify_issues_api_key(unauthenticated_client, monkeypatch):
    captured: dict[str, str] = {}

    async def fake_send(phone: str, code: str) -> None:
        captured["code"] = code

    monkeypatch.setattr("app.api.auth.send_verification_sms", fake_send)

    send_res = await unauthenticated_client.post("/api/auth/send-code", json={"phone": "+821012345678"})
    assert send_res.status_code == 200
    assert send_res.json() == {"sent": True}
    assert "code" in captured

    verify_res = await unauthenticated_client.post(
        "/api/auth/verify-code", json={"phone": "+821012345678", "code": captured["code"]}
    )
    assert verify_res.status_code == 200
    api_key = verify_res.json()["api_key"]
    assert api_key.startswith("sk-")


@pytest.mark.asyncio
async def test_verify_code_wrong_code_returns_400(unauthenticated_client, monkeypatch):
    monkeypatch.setattr("app.api.auth.send_verification_sms", AsyncMock(return_value=None))
    await unauthenticated_client.post("/api/auth/send-code", json={"phone": "+821000000001"})

    res = await unauthenticated_client.post(
        "/api/auth/verify-code", json={"phone": "+821000000001", "code": "000000"}
    )
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_verify_code_expired_returns_400(unauthenticated_client, db_session, monkeypatch):
    captured: dict[str, str] = {}

    async def fake_send(phone: str, code: str) -> None:
        captured["code"] = code

    monkeypatch.setattr("app.api.auth.send_verification_sms", fake_send)
    await unauthenticated_client.post("/api/auth/send-code", json={"phone": "+821000000002"})

    verification = await user_crud.get_pending_verification(db_session, "+821000000002")
    verification.expires_at = user_crud.utcnow_naive().replace(year=2000)
    await db_session.commit()

    res = await unauthenticated_client.post(
        "/api/auth/verify-code", json={"phone": "+821000000002", "code": captured["code"]}
    )
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_verify_code_locks_out_after_max_attempts(unauthenticated_client, monkeypatch):
    monkeypatch.setattr("app.api.auth.send_verification_sms", AsyncMock(return_value=None))
    await unauthenticated_client.post("/api/auth/send-code", json={"phone": "+821000000003"})

    for _ in range(OTP_MAX_ATTEMPTS):
        res = await unauthenticated_client.post(
            "/api/auth/verify-code", json={"phone": "+821000000003", "code": "111111"}
        )
        assert res.status_code == 400

    # Even the code is now gone (deleted once the attempt cap was hit) — a request with
    # any code, right or wrong, is rejected rather than silently retried forever.
    res = await unauthenticated_client.post(
        "/api/auth/verify-code", json={"phone": "+821000000003", "code": "111111"}
    )
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_send_code_rate_limited_on_resend(unauthenticated_client, monkeypatch):
    monkeypatch.setattr("app.api.auth.send_verification_sms", AsyncMock(return_value=None))
    first = await unauthenticated_client.post("/api/auth/send-code", json={"phone": "+821000000004"})
    assert first.status_code == 200

    second = await unauthenticated_client.post("/api/auth/send-code", json={"phone": "+821000000004"})
    assert second.status_code == 429


@pytest.mark.asyncio
async def test_login_with_api_key_returns_token(unauthenticated_client, monkeypatch):
    captured: dict[str, str] = {}

    async def fake_send(phone: str, code: str) -> None:
        captured["code"] = code

    monkeypatch.setattr("app.api.auth.send_verification_sms", fake_send)
    await unauthenticated_client.post("/api/auth/send-code", json={"phone": "+821000000005"})
    verify_res = await unauthenticated_client.post(
        "/api/auth/verify-code", json={"phone": "+821000000005", "code": captured["code"]}
    )
    api_key = verify_res.json()["api_key"]

    login_res = await unauthenticated_client.post("/api/auth/login-with-api-key", json={"api_key": api_key})
    assert login_res.status_code == 200
    assert login_res.json()["token_type"] == "bearer"
    assert login_res.json()["access_token"]


@pytest.mark.asyncio
async def test_login_with_invalid_api_key_returns_401(unauthenticated_client):
    res = await unauthenticated_client.post("/api/auth/login-with-api-key", json={"api_key": "sk-not-real"})
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_user_token_unlocks_main_api_but_not_admin_api(unauthenticated_client, monkeypatch):
    captured: dict[str, str] = {}

    async def fake_send(phone: str, code: str) -> None:
        captured["code"] = code

    monkeypatch.setattr("app.api.auth.send_verification_sms", fake_send)
    await unauthenticated_client.post("/api/auth/send-code", json={"phone": "+821000000006"})
    verify_res = await unauthenticated_client.post(
        "/api/auth/verify-code", json={"phone": "+821000000006", "code": captured["code"]}
    )
    api_key = verify_res.json()["api_key"]
    login_res = await unauthenticated_client.post("/api/auth/login-with-api-key", json={"api_key": api_key})
    token = login_res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    accounts_res = await unauthenticated_client.get("/api/accounts", headers=headers)
    assert accounts_res.status_code == 200

    admin_only_res = await unauthenticated_client.get("/api/admin/api-keys", headers=headers)
    assert admin_only_res.status_code == 401


@pytest.mark.asyncio
async def test_user_token_rejected_after_deactivation(unauthenticated_client, db_session, monkeypatch):
    captured: dict[str, str] = {}

    async def fake_send(phone: str, code: str) -> None:
        captured["code"] = code

    monkeypatch.setattr("app.api.auth.send_verification_sms", fake_send)
    await unauthenticated_client.post("/api/auth/send-code", json={"phone": "+821000000007"})
    verify_res = await unauthenticated_client.post(
        "/api/auth/verify-code", json={"phone": "+821000000007", "code": captured["code"]}
    )
    api_key = verify_res.json()["api_key"]
    login_res = await unauthenticated_client.post("/api/auth/login-with-api-key", json={"api_key": api_key})
    token = login_res.json()["access_token"]

    user = await user_crud.get_user_by_phone(db_session, "+821000000007")
    await user_crud.set_active(db_session, user, False)

    res = await unauthenticated_client.get("/api/accounts", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_auth_me_reports_admin_role(unauthenticated_client):
    from app.config import settings

    login = await unauthenticated_client.post(
        "/api/admin/login", json={"username": settings.admin_username, "password": settings.admin_password}
    )
    token = login.json()["access_token"]

    res = await unauthenticated_client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    assert res.json() == {"role": "admin", "phone": None}


@pytest.mark.asyncio
async def test_auth_me_reports_user_role_and_phone(unauthenticated_client, monkeypatch):
    captured: dict[str, str] = {}

    async def fake_send(phone: str, code: str) -> None:
        captured["code"] = code

    monkeypatch.setattr("app.api.auth.send_verification_sms", fake_send)
    await unauthenticated_client.post("/api/auth/send-code", json={"phone": "+821000000008"})
    verify_res = await unauthenticated_client.post(
        "/api/auth/verify-code", json={"phone": "+821000000008", "code": captured["code"]}
    )
    api_key = verify_res.json()["api_key"]
    login_res = await unauthenticated_client.post("/api/auth/login-with-api-key", json={"api_key": api_key})
    token = login_res.json()["access_token"]

    res = await unauthenticated_client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    assert res.json() == {"role": "user", "phone": "+821000000008"}


@pytest.mark.asyncio
async def test_auth_me_reports_api_key_role(unauthenticated_client):
    from app.config import settings

    admin_login = await unauthenticated_client.post(
        "/api/admin/login", json={"username": settings.admin_username, "password": settings.admin_password}
    )
    admin_token = admin_login.json()["access_token"]
    created = await unauthenticated_client.post(
        "/api/admin/api-keys",
        json={"name": "test"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    raw_key = created.json()["key"]

    res = await unauthenticated_client.get("/api/auth/me", headers={"X-API-Key": raw_key})
    assert res.status_code == 200
    assert res.json() == {"role": "api_key", "phone": None}
