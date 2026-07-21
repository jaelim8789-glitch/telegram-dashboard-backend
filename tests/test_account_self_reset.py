from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telethon.errors import PasswordHashInvalidError, PhoneCodeInvalidError, SessionPasswordNeededError

from app.core.rate_limiter import reset_rate_limits

PHONE = "+821099998888"


@pytest.fixture(autouse=True)
def _clean_rate_limits():
    reset_rate_limits()
    yield
    reset_rate_limits()


async def _create_stale_account(client, phone=PHONE):
    res = await client.post("/api/accounts", json={"phone": phone})
    assert res.status_code == 201
    return res.json()["id"]


@pytest.mark.asyncio
async def test_send_code_success(client, monkeypatch):
    fake_client = SimpleNamespace(
        send_code_request=AsyncMock(return_value=SimpleNamespace(phone_code_hash="hash123")),
    )
    monkeypatch.setattr("app.api.account_self_reset.pool.get_client", AsyncMock(return_value=fake_client))
    set_pending_auth = Mock()
    monkeypatch.setattr("app.api.account_self_reset.pool.set_pending_auth", set_pending_auth)

    res = await client.post("/api/accounts/self-reset/send-code", json={"phone": PHONE})
    assert res.status_code == 200
    assert res.json()["reset"] is False
    set_pending_auth.assert_called_once_with(f"self-reset:{PHONE}", "hash123")


@pytest.mark.asyncio
async def test_send_code_rate_limited(client, monkeypatch):
    fake_client = SimpleNamespace(
        send_code_request=AsyncMock(return_value=SimpleNamespace(phone_code_hash="hash123")),
    )
    monkeypatch.setattr("app.api.account_self_reset.pool.get_client", AsyncMock(return_value=fake_client))
    monkeypatch.setattr("app.api.account_self_reset.pool.set_pending_auth", Mock())

    for _ in range(5):
        res = await client.post("/api/accounts/self-reset/send-code", json={"phone": PHONE})
        assert res.status_code == 200

    res = await client.post("/api/accounts/self-reset/send-code", json={"phone": PHONE})
    assert res.status_code == 429


@pytest.mark.asyncio
async def test_verify_code_without_send_code_returns_400(client):
    res = await client.post("/api/accounts/self-reset/verify-code", json={"phone": PHONE, "code": "12345"})
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_verify_code_wrong_code_does_not_delete_account(client, monkeypatch, db_session):
    from app.crud import account as account_crud

    account_id = await _create_stale_account(client)

    get_pending_auth = Mock(return_value=SimpleNamespace(phone_code_hash="hash123"))
    monkeypatch.setattr("app.api.account_self_reset.pool.get_pending_auth", get_pending_auth)

    fake_client = SimpleNamespace(sign_in=AsyncMock(side_effect=PhoneCodeInvalidError(request=None)))
    monkeypatch.setattr("app.api.account_self_reset.pool.get_client", AsyncMock(return_value=fake_client))

    res = await client.post("/api/accounts/self-reset/verify-code", json={"phone": PHONE, "code": "00000"})
    assert res.status_code == 400

    # Wrong code must NOT wipe the account — that's the whole security point.
    assert await account_crud.get_account(db_session, account_id) is not None


@pytest.mark.asyncio
async def test_verify_code_success_deletes_stale_account_and_allows_reregistration(client, monkeypatch, db_session):
    from app.crud import account as account_crud

    account_id = await _create_stale_account(client)

    get_pending_auth = Mock(return_value=SimpleNamespace(phone_code_hash="hash123"))
    monkeypatch.setattr("app.api.account_self_reset.pool.get_pending_auth", get_pending_auth)
    monkeypatch.setattr("app.api.account_self_reset.pool.clear_pending_auth", Mock())
    monkeypatch.setattr("app.api.account_self_reset.pool.remove_client", AsyncMock())

    fake_client = SimpleNamespace(sign_in=AsyncMock(return_value=None))
    monkeypatch.setattr("app.api.account_self_reset.pool.get_client", AsyncMock(return_value=fake_client))

    res = await client.post("/api/accounts/self-reset/verify-code", json={"phone": PHONE, "code": "12345"})
    assert res.status_code == 200
    assert res.json()["reset"] is True

    assert await account_crud.get_account(db_session, account_id) is None

    # Now re-registering the same phone must succeed instead of 409ing.
    res2 = await client.post("/api/accounts", json={"phone": PHONE})
    assert res2.status_code == 201


@pytest.mark.asyncio
async def test_verify_code_requires_2fa_does_not_delete_yet(client, monkeypatch, db_session):
    from app.crud import account as account_crud

    account_id = await _create_stale_account(client)

    get_pending_auth = Mock(return_value=SimpleNamespace(phone_code_hash="hash123"))
    monkeypatch.setattr("app.api.account_self_reset.pool.get_pending_auth", get_pending_auth)

    fake_client = SimpleNamespace(sign_in=AsyncMock(side_effect=SessionPasswordNeededError(request=None)))
    monkeypatch.setattr("app.api.account_self_reset.pool.get_client", AsyncMock(return_value=fake_client))

    res = await client.post("/api/accounts/self-reset/verify-code", json={"phone": PHONE, "code": "12345"})
    assert res.status_code == 200
    body = res.json()
    assert body["reset"] is False
    assert body["requires_2fa"] is True

    # Not verified yet — account must survive until 2FA also succeeds.
    assert await account_crud.get_account(db_session, account_id) is not None


@pytest.mark.asyncio
async def test_verify_2fa_wrong_password_does_not_delete(client, monkeypatch, db_session):
    from app.crud import account as account_crud

    account_id = await _create_stale_account(client)

    fake_client = SimpleNamespace(sign_in=AsyncMock(side_effect=PasswordHashInvalidError(request=None)))
    monkeypatch.setattr("app.api.account_self_reset.pool.get_client", AsyncMock(return_value=fake_client))

    res = await client.post("/api/accounts/self-reset/verify-2fa", json={"phone": PHONE, "password": "wrong"})
    assert res.status_code == 400
    assert await account_crud.get_account(db_session, account_id) is not None


@pytest.mark.asyncio
async def test_verify_2fa_success_deletes_stale_account(client, monkeypatch, db_session):
    from app.crud import account as account_crud

    account_id = await _create_stale_account(client)

    monkeypatch.setattr("app.api.account_self_reset.pool.clear_pending_auth", Mock())
    monkeypatch.setattr("app.api.account_self_reset.pool.remove_client", AsyncMock())
    fake_client = SimpleNamespace(sign_in=AsyncMock(return_value=None))
    monkeypatch.setattr("app.api.account_self_reset.pool.get_client", AsyncMock(return_value=fake_client))

    res = await client.post("/api/accounts/self-reset/verify-2fa", json={"phone": PHONE, "password": "correct"})
    assert res.status_code == 200
    assert res.json()["reset"] is True
    assert await account_crud.get_account(db_session, account_id) is None


@pytest.mark.asyncio
async def test_verify_code_no_stale_account_still_succeeds_idempotently(client, monkeypatch):
    """Nothing to delete (no conflicting row) shouldn't error — this endpoint is
    reachable any time a user wants to prove ownership, not only on a 409."""
    get_pending_auth = Mock(return_value=SimpleNamespace(phone_code_hash="hash123"))
    monkeypatch.setattr("app.api.account_self_reset.pool.get_pending_auth", get_pending_auth)
    monkeypatch.setattr("app.api.account_self_reset.pool.clear_pending_auth", Mock())
    monkeypatch.setattr("app.api.account_self_reset.pool.remove_client", AsyncMock())

    fake_client = SimpleNamespace(sign_in=AsyncMock(return_value=None))
    monkeypatch.setattr("app.api.account_self_reset.pool.get_client", AsyncMock(return_value=fake_client))

    res = await client.post("/api/accounts/self-reset/verify-code", json={"phone": "+821000000001", "code": "12345"})
    assert res.status_code == 200
    assert res.json()["reset"] is True
