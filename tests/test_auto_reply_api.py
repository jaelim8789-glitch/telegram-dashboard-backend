from unittest.mock import AsyncMock

import pytest

from app.crud import account as account_crud
from app.schemas.account import AccountCreate


async def _make_account(db_session, phone="+821033334444"):
    return await account_crud.create_account(db_session, AccountCreate(phone=phone))


def _rule_payload(**overrides):
    payload = {
        "name": "가격 문의",
        "match_type": "keyword",
        "match_value": "가격",
        "reply_content": "가격은 10,000원입니다",
        "cooldown_hours": 1,
        "max_replies_per_day": 100,
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_create_rule_returns_201(client, db_session):
    account = await _make_account(db_session)

    response = await client.post(f"/api/accounts/{account.id}/auto-reply", json=_rule_payload())

    assert response.status_code == 201
    body = response.json()
    assert body["account_id"] == account.id
    assert body["match_value"] == "가격"
    assert body["is_active"] is True


@pytest.mark.asyncio
async def test_create_rule_for_missing_account_returns_404(client):
    response = await client.post("/api/accounts/does-not-exist/auto-reply", json=_rule_payload())
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_read_settings_bundles_master_switch_and_rules(client, db_session):
    account = await _make_account(db_session)
    await client.post(f"/api/accounts/{account.id}/auto-reply", json=_rule_payload())

    response = await client.get(f"/api/accounts/{account.id}/auto-reply")

    assert response.status_code == 200
    body = response.json()
    assert body["auto_reply_enabled"] is False
    assert len(body["rules"]) == 1


@pytest.mark.asyncio
async def test_update_rule(client, db_session):
    account = await _make_account(db_session)
    created = (await client.post(f"/api/accounts/{account.id}/auto-reply", json=_rule_payload())).json()

    response = await client.put(
        f"/api/accounts/{account.id}/auto-reply/{created['id']}", json={"reply_content": "영업 시간은 9시부터 6시까지입니다"}
    )

    assert response.status_code == 200
    assert response.json()["reply_content"] == "영업 시간은 9시부터 6시까지입니다"
    assert response.json()["match_value"] == "가격"  # untouched fields survive a partial update


@pytest.mark.asyncio
async def test_update_rule_not_found_returns_404(client, db_session):
    account = await _make_account(db_session)
    response = await client.put(f"/api/accounts/{account.id}/auto-reply/does-not-exist", json={"name": "x"})
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_delete_rule(client, db_session):
    account = await _make_account(db_session)
    created = (await client.post(f"/api/accounts/{account.id}/auto-reply", json=_rule_payload())).json()

    delete_response = await client.delete(f"/api/accounts/{account.id}/auto-reply/{created['id']}")
    assert delete_response.status_code == 204

    settings_response = await client.get(f"/api/accounts/{account.id}/auto-reply")
    assert settings_response.json()["rules"] == []


@pytest.mark.asyncio
async def test_toggle_on_calls_enable_and_returns_enabled_true(client, db_session, monkeypatch):
    account = await _make_account(db_session)
    enable_mock = AsyncMock()
    monkeypatch.setattr("app.api.auto_reply.enable_auto_reply", enable_mock)

    response = await client.post(f"/api/accounts/{account.id}/auto-reply/toggle", json={"enabled": True})

    assert response.status_code == 200
    assert response.json() == {"account_id": account.id, "auto_reply_enabled": True}
    enable_mock.assert_awaited_once_with(account.id)


@pytest.mark.asyncio
async def test_toggle_on_unauthenticated_account_returns_400(client, db_session, monkeypatch):
    from app.services.auto_reply_service import AccountNotAuthenticatedError

    account = await _make_account(db_session)
    monkeypatch.setattr(
        "app.api.auto_reply.enable_auto_reply", AsyncMock(side_effect=AccountNotAuthenticatedError("인증 필요"))
    )

    response = await client.post(f"/api/accounts/{account.id}/auto-reply/toggle", json={"enabled": True})

    assert response.status_code == 400
    assert response.json()["detail"] == "인증 필요"


@pytest.mark.asyncio
async def test_toggle_off_calls_disable(client, db_session, monkeypatch):
    account = await _make_account(db_session)
    disable_mock = AsyncMock()
    monkeypatch.setattr("app.api.auto_reply.disable_auto_reply", disable_mock)

    response = await client.post(f"/api/accounts/{account.id}/auto-reply/toggle", json={"enabled": False})

    assert response.status_code == 200
    disable_mock.assert_awaited_once_with(account.id)


@pytest.mark.asyncio
async def test_read_logs_returns_empty_list_initially(client, db_session):
    account = await _make_account(db_session)
    response = await client.get(f"/api/accounts/{account.id}/auto-reply/logs")
    assert response.status_code == 200
    assert response.json() == []
