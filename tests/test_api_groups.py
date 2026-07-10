from unittest.mock import AsyncMock

import pytest

from app.services.telegram_actions import AccountNotAuthenticatedError


async def _create_account(client, phone="+821099990000"):
    res = await client.post("/api/accounts", json={"phone": phone})
    assert res.status_code == 201
    return res.json()["id"]


def _fake_groups():
    return [
        {"id": "-100111", "title": "연구용 그룹", "type": "group", "participants_count": 12},
        {"id": "-100222", "title": "테스트 채널", "type": "channel", "participants_count": None},
        {"id": "-100333", "title": "개발자 모임", "type": "megagroup", "participants_count": 500},
    ]


@pytest.mark.asyncio
async def test_groups_unauthenticated_account_returns_400(client):
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
    monkeypatch.setattr("app.api.groups.list_groups", AsyncMock(return_value=_fake_groups()))
    res = await client.get(f"/api/accounts/{account_id}/groups")
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 3
    assert len(body["items"]) == 3
    assert "total_pages" in body


@pytest.mark.asyncio
async def test_groups_search_filter(client, monkeypatch):
    account_id = await _create_account(client)
    monkeypatch.setattr("app.api.groups.list_groups", AsyncMock(return_value=_fake_groups()))
    res = await client.get(f"/api/accounts/{account_id}/groups?search=연구")
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 1
    assert body["items"][0]["title"] == "연구용 그룹"


@pytest.mark.asyncio
async def test_groups_type_filter(client, monkeypatch):
    account_id = await _create_account(client)
    monkeypatch.setattr("app.api.groups.list_groups", AsyncMock(return_value=_fake_groups()))
    res = await client.get(f"/api/accounts/{account_id}/groups?type=channel")
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 1
    assert body["items"][0]["type"] == "channel"


@pytest.mark.asyncio
async def test_groups_pagination(client, monkeypatch):
    account_id = await _create_account(client)
    monkeypatch.setattr("app.api.groups.list_groups", AsyncMock(return_value=_fake_groups()))
    res = await client.get(f"/api/accounts/{account_id}/groups?page=1&page_size=2")
    assert res.status_code == 200
    body = res.json()
    assert len(body["items"]) == 2
    assert body["total"] == 3
    assert body["total_pages"] == 2


@pytest.mark.asyncio
async def test_groups_discovery_info(client, monkeypatch):
    account_id = await _create_account(client)
    monkeypatch.setattr("app.api.groups.list_groups", AsyncMock(return_value=_fake_groups()))
    res = await client.get(f"/api/accounts/{account_id}/groups/discovery-info")
    assert res.status_code == 200
    body = res.json()
    assert body["total_groups"] == 3
    assert body["groups"] == 2
    assert body["channels"] == 1


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
