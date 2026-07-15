from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
    UserDeactivatedBanError,
)

from app.core.crypto import decrypt_session, encrypt_session


async def _create_account(client, phone="+821012345678"):
    res = await client.post("/api/accounts", json={"phone": phone})
    assert res.status_code == 201
    return res.json()["id"]


def _flood_wait_error():
    # FloodWaitError's __init__ expects an RPC request/response context; easiest to build
    # a real instance is via its documented seconds-only path through .__reduce__ isn't
    # available, so construct the minimal object it actually reads: `.seconds`.
    err = FloodWaitError.__new__(FloodWaitError)
    err.seconds = 5
    return err


@pytest.mark.asyncio
async def test_send_code_success(client, monkeypatch):
    account_id = await _create_account(client)

    fake_client = SimpleNamespace(
        send_code_request=AsyncMock(return_value=SimpleNamespace(phone_code_hash="hash123")),
        session=SimpleNamespace(save=Mock(return_value="session-string")),
    )
    monkeypatch.setattr("app.api.telegram_auth.pool.get_client", AsyncMock(return_value=fake_client))
    set_pending_auth = Mock()
    monkeypatch.setattr("app.api.telegram_auth.pool.set_pending_auth", set_pending_auth)

    res = await client.post(f"/api/accounts/{account_id}/send-code")
    assert res.status_code == 200
    assert res.json() == {"sent": True}
    set_pending_auth.assert_called_once_with(account_id, "hash123")


@pytest.mark.asyncio
async def test_send_code_account_not_found(client):
    res = await client.post("/api/accounts/does-not-exist/send-code")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_send_code_not_configured_returns_503(client, monkeypatch):
    account_id = await _create_account(client)
    monkeypatch.setattr(
        "app.api.telegram_auth.pool.get_client", AsyncMock(side_effect=RuntimeError("설정되지 않았습니다"))
    )

    res = await client.post(f"/api/accounts/{account_id}/send-code")
    assert res.status_code == 503


@pytest.mark.asyncio
async def test_send_code_invalid_phone_returns_400(client, monkeypatch):
    account_id = await _create_account(client)
    fake_client = SimpleNamespace(send_code_request=AsyncMock(side_effect=PhoneNumberInvalidError(Mock())))
    monkeypatch.setattr("app.api.telegram_auth.pool.get_client", AsyncMock(return_value=fake_client))

    res = await client.post(f"/api/accounts/{account_id}/send-code")
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_send_code_flood_wait_returns_429(client, monkeypatch):
    account_id = await _create_account(client)
    fake_client = SimpleNamespace(send_code_request=AsyncMock(side_effect=_flood_wait_error()))
    monkeypatch.setattr("app.api.telegram_auth.pool.get_client", AsyncMock(return_value=fake_client))

    res = await client.post(f"/api/accounts/{account_id}/send-code")
    assert res.status_code == 429


@pytest.mark.asyncio
async def test_send_code_banned_account(client, monkeypatch):
    account_id = await _create_account(client)
    fake_client = SimpleNamespace(send_code_request=AsyncMock(side_effect=UserDeactivatedBanError(Mock())))
    monkeypatch.setattr("app.api.telegram_auth.pool.get_client", AsyncMock(return_value=fake_client))

    res = await client.post(f"/api/accounts/{account_id}/send-code")
    assert res.status_code == 403

    account = await client.get(f"/api/accounts/{account_id}")
    assert account.json()["status"] == "banned"


@pytest.mark.asyncio
async def test_send_code_dead_session_self_heals(client, db_session, monkeypatch):
    """A previously-persisted session that Telegram has since revoked must be
    cleared, not silently reused forever, so the user's retry actually starts
    from a blank client."""
    from app.crud import account as account_crud
    from telethon.errors import AuthKeyUnregisteredError

    account_id = await _create_account(client)
    account = await account_crud.get_account(db_session, account_id)
    account.session_data = encrypt_session("revoked-session-string")
    await db_session.commit()

    fake_client = SimpleNamespace(send_code_request=AsyncMock(side_effect=AuthKeyUnregisteredError(Mock())))
    monkeypatch.setattr("app.api.telegram_auth.pool.get_client", AsyncMock(return_value=fake_client))
    remove_client = AsyncMock()
    monkeypatch.setattr("app.api.telegram_auth.pool.remove_client", remove_client)

    res = await client.post(f"/api/accounts/{account_id}/send-code")
    assert res.status_code == 400
    remove_client.assert_called_once_with(account_id)

    account = await account_crud.get_account(db_session, account_id)
    assert account.session_data is None


@pytest.mark.asyncio
async def test_send_code_persists_session_snapshot(client, db_session, monkeypatch):
    """The auth_key negotiated during send-code is persisted immediately, so a
    process restart before verify-code can reconnect instead of starting blank."""
    from app.crud import account as account_crud

    account_id = await _create_account(client)
    fake_client = SimpleNamespace(
        send_code_request=AsyncMock(return_value=SimpleNamespace(phone_code_hash="hash123")),
        session=SimpleNamespace(save=Mock(return_value="pre-auth-session-string")),
    )
    monkeypatch.setattr("app.api.telegram_auth.pool.get_client", AsyncMock(return_value=fake_client))
    monkeypatch.setattr("app.api.telegram_auth.pool.set_pending_auth", Mock())

    res = await client.post(f"/api/accounts/{account_id}/send-code")
    assert res.status_code == 200

    account = await account_crud.get_account(db_session, account_id)
    assert account.session_data is not None
    assert decrypt_session(account.session_data) == "pre-auth-session-string"


@pytest.mark.asyncio
async def test_send_code_passes_existing_session_to_pool(client, db_session, monkeypatch):
    """If a session was already persisted (e.g. a retry after a restart), send-code
    must hand it to the pool instead of requesting a blank client."""
    from app.crud import account as account_crud

    account_id = await _create_account(client)
    account = await account_crud.get_account(db_session, account_id)
    account.session_data = encrypt_session("existing-session-string")
    await db_session.commit()

    fake_client = SimpleNamespace(
        send_code_request=AsyncMock(return_value=SimpleNamespace(phone_code_hash="hash123")),
        session=SimpleNamespace(save=Mock(return_value="existing-session-string")),
    )
    get_client_mock = AsyncMock(return_value=fake_client)
    monkeypatch.setattr("app.api.telegram_auth.pool.get_client", get_client_mock)
    monkeypatch.setattr("app.api.telegram_auth.pool.set_pending_auth", Mock())

    res = await client.post(f"/api/accounts/{account_id}/send-code")
    assert res.status_code == 200
    get_client_mock.assert_called_once_with(account_id, "existing-session-string")


@pytest.mark.asyncio
async def test_verify_code_without_pending_auth_returns_400(client):
    account_id = await _create_account(client)
    res = await client.post(f"/api/accounts/{account_id}/verify-code", json={"code": "12345"})
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_verify_code_success(client, monkeypatch):
    account_id = await _create_account(client)
    monkeypatch.setattr(
        "app.api.telegram_auth.pool.get_pending_auth",
        Mock(return_value=SimpleNamespace(phone_code_hash="hash123")),
    )
    fake_client = SimpleNamespace(
        sign_in=AsyncMock(return_value=None),
        session=SimpleNamespace(save=Mock(return_value="session-string")),
    )
    monkeypatch.setattr("app.api.telegram_auth.pool.get_client", AsyncMock(return_value=fake_client))
    monkeypatch.setattr("app.api.telegram_auth.pool.clear_pending_auth", Mock())

    res = await client.post(f"/api/accounts/{account_id}/verify-code", json={"code": "12345"})
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "active"
    assert body["requires_2fa"] is False

    account = await client.get(f"/api/accounts/{account_id}")
    assert account.json()["status"] == "active"


@pytest.mark.asyncio
async def test_verify_code_requires_2fa(client, monkeypatch):
    account_id = await _create_account(client)
    monkeypatch.setattr(
        "app.api.telegram_auth.pool.get_pending_auth",
        Mock(return_value=SimpleNamespace(phone_code_hash="hash123")),
    )
    fake_client = SimpleNamespace(
        sign_in=AsyncMock(side_effect=SessionPasswordNeededError(Mock())),
        session=SimpleNamespace(save=Mock(return_value="session-string")),
    )
    monkeypatch.setattr("app.api.telegram_auth.pool.get_client", AsyncMock(return_value=fake_client))

    res = await client.post(f"/api/accounts/{account_id}/verify-code", json={"code": "12345"})
    assert res.status_code == 200
    assert res.json()["requires_2fa"] is True


@pytest.mark.asyncio
async def test_verify_code_persists_session_before_2fa(client, db_session, monkeypatch):
    """The auth_key survives a restart between verify-code (2FA required) and
    verify-2fa: it must be saved even though the account isn't "active" yet."""
    from app.crud import account as account_crud

    account_id = await _create_account(client)
    monkeypatch.setattr(
        "app.api.telegram_auth.pool.get_pending_auth",
        Mock(return_value=SimpleNamespace(phone_code_hash="hash123")),
    )
    fake_client = SimpleNamespace(
        sign_in=AsyncMock(side_effect=SessionPasswordNeededError(Mock())),
        session=SimpleNamespace(save=Mock(return_value="mid-flow-session-string")),
    )
    monkeypatch.setattr("app.api.telegram_auth.pool.get_client", AsyncMock(return_value=fake_client))

    res = await client.post(f"/api/accounts/{account_id}/verify-code", json={"code": "12345"})
    assert res.status_code == 200

    account = await account_crud.get_account(db_session, account_id)
    assert account.session_data is not None
    assert decrypt_session(account.session_data) == "mid-flow-session-string"
    assert account.status != "active"  # 2FA still pending — status must not jump ahead


@pytest.mark.asyncio
async def test_verify_code_invalid_code_returns_400(client, monkeypatch):
    account_id = await _create_account(client)
    monkeypatch.setattr(
        "app.api.telegram_auth.pool.get_pending_auth",
        Mock(return_value=SimpleNamespace(phone_code_hash="hash123")),
    )
    fake_client = SimpleNamespace(sign_in=AsyncMock(side_effect=PhoneCodeInvalidError(Mock())))
    monkeypatch.setattr("app.api.telegram_auth.pool.get_client", AsyncMock(return_value=fake_client))

    res = await client.post(f"/api/accounts/{account_id}/verify-code", json={"code": "00000"})
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_verify_code_expired_clears_pending_auth(client, monkeypatch):
    account_id = await _create_account(client)
    monkeypatch.setattr(
        "app.api.telegram_auth.pool.get_pending_auth",
        Mock(return_value=SimpleNamespace(phone_code_hash="hash123")),
    )
    fake_client = SimpleNamespace(sign_in=AsyncMock(side_effect=PhoneCodeExpiredError(Mock())))
    monkeypatch.setattr("app.api.telegram_auth.pool.get_client", AsyncMock(return_value=fake_client))
    clear_pending_auth = Mock()
    monkeypatch.setattr("app.api.telegram_auth.pool.clear_pending_auth", clear_pending_auth)

    res = await client.post(f"/api/accounts/{account_id}/verify-code", json={"code": "12345"})
    assert res.status_code == 400
    clear_pending_auth.assert_called_once_with(account_id)


@pytest.mark.asyncio
async def test_verify_code_flood_wait_returns_429(client, monkeypatch):
    account_id = await _create_account(client)
    monkeypatch.setattr(
        "app.api.telegram_auth.pool.get_pending_auth",
        Mock(return_value=SimpleNamespace(phone_code_hash="hash123")),
    )
    fake_client = SimpleNamespace(sign_in=AsyncMock(side_effect=_flood_wait_error()))
    monkeypatch.setattr("app.api.telegram_auth.pool.get_client", AsyncMock(return_value=fake_client))

    res = await client.post(f"/api/accounts/{account_id}/verify-code", json={"code": "12345"})
    assert res.status_code == 429


@pytest.mark.asyncio
async def test_verify_code_dead_session_self_heals(client, db_session, monkeypatch):
    """A restart between send-code and verify-code (with no persisted session to
    fall back on) leaves the pool handing back a blank, unauthenticated client.
    Telegram then rejects the sign-in attempt with an auth-key error instead of
    the expected PhoneCode* error. This must surface as an actionable 400 and
    leave the account able to restart cleanly from send-code, not stuck."""
    from app.crud import account as account_crud
    from telethon.errors import AuthKeyUnregisteredError

    account_id = await _create_account(client)
    account = await account_crud.get_account(db_session, account_id)
    account.session_data = encrypt_session("stale-session-string")
    account.status = "inactive"
    await db_session.commit()

    monkeypatch.setattr(
        "app.api.telegram_auth.pool.get_pending_auth",
        Mock(return_value=SimpleNamespace(phone_code_hash="hash123")),
    )
    fake_client = SimpleNamespace(sign_in=AsyncMock(side_effect=AuthKeyUnregisteredError(Mock())))
    monkeypatch.setattr("app.api.telegram_auth.pool.get_client", AsyncMock(return_value=fake_client))
    remove_client = AsyncMock()
    monkeypatch.setattr("app.api.telegram_auth.pool.remove_client", remove_client)

    res = await client.post(f"/api/accounts/{account_id}/verify-code", json={"code": "12345"})
    assert res.status_code == 400
    assert "처음부터" in res.json()["detail"]
    remove_client.assert_called_once_with(account_id)

    account = await account_crud.get_account(db_session, account_id)
    assert account.session_data is None
    assert account.status == "inactive"


@pytest.mark.asyncio
async def test_verify_2fa_success(client, monkeypatch):
    account_id = await _create_account(client)
    fake_client = SimpleNamespace(
        sign_in=AsyncMock(return_value=None),
        session=SimpleNamespace(save=Mock(return_value="session-string")),
    )
    monkeypatch.setattr("app.api.telegram_auth.pool.get_client", AsyncMock(return_value=fake_client))
    monkeypatch.setattr("app.api.telegram_auth.pool.clear_pending_auth", Mock())

    res = await client.post(f"/api/accounts/{account_id}/verify-2fa", json={"password": "hunter2"})
    assert res.status_code == 200
    assert res.json()["status"] == "active"


@pytest.mark.asyncio
async def test_verify_2fa_wrong_password_returns_400(client, monkeypatch):
    account_id = await _create_account(client)
    fake_client = SimpleNamespace(sign_in=AsyncMock(side_effect=PasswordHashInvalidError(Mock())))
    monkeypatch.setattr("app.api.telegram_auth.pool.get_client", AsyncMock(return_value=fake_client))

    res = await client.post(f"/api/accounts/{account_id}/verify-2fa", json={"password": "wrong"})
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_verify_2fa_passes_persisted_session_to_pool(client, db_session, monkeypatch):
    """This is the actual production scenario that motivated the fix: the backend
    restarts between verify-code (2FA required) and verify-2fa, wiping the pool's
    in-memory clients. verify-2fa must hand the pool the session persisted by
    verify-code so it reconnects with the same auth_key instead of a blank one."""
    from app.crud import account as account_crud

    account_id = await _create_account(client)
    account = await account_crud.get_account(db_session, account_id)
    account.session_data = encrypt_session("persisted-after-verify-code")
    await db_session.commit()

    fake_client = SimpleNamespace(
        sign_in=AsyncMock(return_value=None),
        session=SimpleNamespace(save=Mock(return_value="fully-authorized-session")),
    )
    get_client_mock = AsyncMock(return_value=fake_client)
    monkeypatch.setattr("app.api.telegram_auth.pool.get_client", get_client_mock)
    monkeypatch.setattr("app.api.telegram_auth.pool.clear_pending_auth", Mock())

    res = await client.post(f"/api/accounts/{account_id}/verify-2fa", json={"password": "hunter2"})
    assert res.status_code == 200
    get_client_mock.assert_called_once_with(account_id, "persisted-after-verify-code")


@pytest.mark.asyncio
async def test_verify_2fa_dead_session_self_heals(client, db_session, monkeypatch):
    from app.crud import account as account_crud
    from telethon.errors import AuthKeyUnregisteredError

    account_id = await _create_account(client)
    account = await account_crud.get_account(db_session, account_id)
    account.session_data = encrypt_session("stale-session-string")
    account.status = "inactive"
    await db_session.commit()

    fake_client = SimpleNamespace(sign_in=AsyncMock(side_effect=AuthKeyUnregisteredError(Mock())))
    monkeypatch.setattr("app.api.telegram_auth.pool.get_client", AsyncMock(return_value=fake_client))
    remove_client = AsyncMock()
    monkeypatch.setattr("app.api.telegram_auth.pool.remove_client", remove_client)

    res = await client.post(f"/api/accounts/{account_id}/verify-2fa", json={"password": "hunter2"})
    assert res.status_code == 400
    assert "처음부터" in res.json()["detail"]
    remove_client.assert_called_once_with(account_id)

    account = await account_crud.get_account(db_session, account_id)
    assert account.session_data is None
    assert account.status == "inactive"


@pytest.mark.asyncio
async def test_status_unauthenticated_account(client):
    account_id = await _create_account(client)
    res = await client.get(f"/api/accounts/{account_id}/status")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "inactive"
    assert "인증되지" in body["detail"]


@pytest.mark.asyncio
async def test_status_authorized_marks_active(client, db_session, monkeypatch):
    from app.crud import account as account_crud

    account_id = await _create_account(client)
    account = await account_crud.get_account(db_session, account_id)
    account.session_data = encrypt_session("fake-session-string")
    await db_session.commit()

    fake_client = SimpleNamespace(is_user_authorized=AsyncMock(return_value=True))
    monkeypatch.setattr("app.api.telegram_auth.pool.get_client", AsyncMock(return_value=fake_client))

    res = await client.get(f"/api/accounts/{account_id}/status")
    assert res.status_code == 200
    assert res.json()["status"] == "active"


@pytest.mark.asyncio
async def test_status_unauthorized_session_marks_inactive(client, db_session, monkeypatch):
    from app.crud import account as account_crud

    account_id = await _create_account(client)
    account = await account_crud.get_account(db_session, account_id)
    account.session_data = encrypt_session("fake-session-string")
    account.status = "active"
    await db_session.commit()

    fake_client = SimpleNamespace(is_user_authorized=AsyncMock(return_value=False))
    monkeypatch.setattr("app.api.telegram_auth.pool.get_client", AsyncMock(return_value=fake_client))

    res = await client.get(f"/api/accounts/{account_id}/status")
    assert res.status_code == 200
    assert res.json()["status"] == "inactive"


@pytest.mark.asyncio
async def test_status_banned_during_check(client, db_session, monkeypatch):
    from app.crud import account as account_crud

    account_id = await _create_account(client)
    account = await account_crud.get_account(db_session, account_id)
    account.session_data = encrypt_session("fake-session-string")
    await db_session.commit()

    fake_client = SimpleNamespace(is_user_authorized=AsyncMock(side_effect=UserDeactivatedBanError(Mock())))
    monkeypatch.setattr("app.api.telegram_auth.pool.get_client", AsyncMock(return_value=fake_client))

    res = await client.get(f"/api/accounts/{account_id}/status")
    assert res.status_code == 200
    assert res.json()["status"] == "banned"


@pytest.mark.asyncio
async def test_status_account_not_found(client):
    res = await client.get("/api/accounts/does-not-exist/status")
    assert res.status_code == 404
