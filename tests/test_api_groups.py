from unittest.mock import AsyncMock

import pytest

from app.services.telegram_actions import AccountNotAuthenticatedError


async def _create_account(client, phone="+821099990000"):
    res = await client.post("/api/accounts", json={"phone": phone})
    assert res.status_code == 201
    return res.json()["id"]


@pytest.mark.asyncio
async def test_groups_unauthenticated_account_returns_400(client):
    # No Telethon mocking needed here: get_authorized_client() raises before ever
    # touching the network, since the account has no session_data yet.
    account_id = await _create_account(client)

    res = await client.get(f"/api/accounts/{account_id}/groups")
    assert res.status_code == 400
    assert "인증" in res.json()["detail"]


@pytest.mark.asyncio
async def test_groups_unknown_account_returns_404(client):
    res = await client.get("/api/accounts/does-not-exist/groups")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_groups_success(client, monkeypatch):
    account_id = await _create_account(client)

    fake_groups = [
        {"id": "-100111", "title": "연구용 그룹", "type": "group", "participants_count": 12},
        {"id": "-100222", "title": "테스트 채널", "type": "channel", "participants_count": None},
    ]
    monkeypatch.setattr("app.api.groups.list_groups", AsyncMock(return_value=fake_groups))

    res = await client.get(f"/api/accounts/{account_id}/groups")
    assert res.status_code == 200
    assert res.json() == fake_groups


@pytest.mark.asyncio
async def test_groups_telegram_not_configured_returns_503(client, monkeypatch):
    account_id = await _create_account(client)

    monkeypatch.setattr(
        "app.api.groups.list_groups",
        AsyncMock(side_effect=RuntimeError("TELEGRAM_API_ID / TELEGRAM_API_HASH가 설정되지 않았습니다.")),
    )

    res = await client.get(f"/api/accounts/{account_id}/groups")
    assert res.status_code == 503


@pytest.mark.asyncio
async def test_groups_session_expired_returns_400(client, monkeypatch):
    account_id = await _create_account(client)

    monkeypatch.setattr(
        "app.api.groups.list_groups",
        AsyncMock(side_effect=AccountNotAuthenticatedError("텔레그램 세션이 만료되었습니다. 다시 인증해주세요.")),
    )

    res = await client.get(f"/api/accounts/{account_id}/groups")
    assert res.status_code == 400
