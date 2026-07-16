"""Regression tests for POST /api/channel-hub/publish.

The Channel Hub tab's "메시지 편집" form always 404'd against production —
the frontend was built against an endpoint that never existed on the backend.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon.errors import ChatWriteForbiddenError, FloodWaitError


async def _create_account(client, phone="+821099990010"):
    res = await client.post("/api/accounts", json={"phone": phone, "name": "채널허브 테스트 계정"})
    assert res.status_code == 201
    return res.json()["id"]


def _fake_client(message_id=555):
    client = AsyncMock()
    client.send_message = AsyncMock(return_value=SimpleNamespace(id=message_id))
    client.pin_message = AsyncMock(return_value=None)
    return client


@pytest.mark.asyncio
async def test_publish_simple_text_message(client, monkeypatch):
    account_id = await _create_account(client)
    fake_client = _fake_client()
    monkeypatch.setattr("app.api.channel_hub.get_authorized_client", AsyncMock(return_value=fake_client))

    res = await client.post(
        "/api/channel-hub/publish",
        json={"account_id": account_id, "channel_id": "-1001234567890", "title": "공지", "body": "안녕하세요"},
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["message_id"] == 555
    assert body["pinned"] is False

    fake_client.send_message.assert_awaited_once()
    call_args = fake_client.send_message.call_args
    assert call_args.args[0] == -1001234567890  # numeric chat id resolved to int
    assert "공지" in call_args.args[1]
    assert "안녕하세요" in call_args.args[1]


@pytest.mark.asyncio
async def test_publish_with_username_channel_id(client, monkeypatch):
    account_id = await _create_account(client)
    fake_client = _fake_client()
    monkeypatch.setattr("app.api.channel_hub.get_authorized_client", AsyncMock(return_value=fake_client))

    res = await client.post(
        "/api/channel-hub/publish",
        json={"account_id": account_id, "channel_id": "@mychannel", "title": "공지"},
    )
    assert res.status_code == 201, res.text
    call_args = fake_client.send_message.call_args
    assert call_args.args[0] == "@mychannel"  # non-numeric passed through as-is


@pytest.mark.asyncio
async def test_publish_with_buttons(client, monkeypatch):
    account_id = await _create_account(client)
    fake_client = _fake_client()
    monkeypatch.setattr("app.api.channel_hub.get_authorized_client", AsyncMock(return_value=fake_client))

    res = await client.post(
        "/api/channel-hub/publish",
        json={
            "account_id": account_id, "channel_id": "-100111", "title": "공지",
            "buttons": [{"label": "바로가기", "url": "https://example.com"}],
        },
    )
    assert res.status_code == 201, res.text
    call_kwargs = fake_client.send_message.call_args.kwargs
    assert call_kwargs["buttons"] is not None


@pytest.mark.asyncio
async def test_publish_with_pin_message(client, monkeypatch):
    account_id = await _create_account(client)
    fake_client = _fake_client(message_id=42)
    monkeypatch.setattr("app.api.channel_hub.get_authorized_client", AsyncMock(return_value=fake_client))

    res = await client.post(
        "/api/channel-hub/publish",
        json={"account_id": account_id, "channel_id": "-100111", "title": "공지", "pin_message": True},
    )
    assert res.status_code == 201, res.text
    assert res.json()["pinned"] is True
    fake_client.pin_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_publish_pin_failure_does_not_fail_the_publish(client, monkeypatch):
    account_id = await _create_account(client)
    fake_client = _fake_client()
    fake_client.pin_message = AsyncMock(side_effect=RuntimeError("no pin permission"))
    monkeypatch.setattr("app.api.channel_hub.get_authorized_client", AsyncMock(return_value=fake_client))

    res = await client.post(
        "/api/channel-hub/publish",
        json={"account_id": account_id, "channel_id": "-100111", "title": "공지", "pin_message": True},
    )
    assert res.status_code == 201, res.text
    assert res.json()["pinned"] is False


@pytest.mark.asyncio
async def test_publish_forbidden_channel_returns_403(client, monkeypatch):
    account_id = await _create_account(client)
    fake_client = _fake_client()
    fake_client.send_message = AsyncMock(side_effect=ChatWriteForbiddenError(request=None))
    monkeypatch.setattr("app.api.channel_hub.get_authorized_client", AsyncMock(return_value=fake_client))

    res = await client.post(
        "/api/channel-hub/publish",
        json={"account_id": account_id, "channel_id": "-100111", "title": "공지"},
    )
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_publish_flood_wait_returns_429(client, monkeypatch):
    account_id = await _create_account(client)
    fake_client = _fake_client()
    fake_client.send_message = AsyncMock(side_effect=FloodWaitError(request=None, capture=30))
    monkeypatch.setattr("app.api.channel_hub.get_authorized_client", AsyncMock(return_value=fake_client))

    res = await client.post(
        "/api/channel-hub/publish",
        json={"account_id": account_id, "channel_id": "-100111", "title": "공지"},
    )
    assert res.status_code == 429


@pytest.mark.asyncio
async def test_publish_unauthenticated_account_returns_400(client, monkeypatch):
    from app.services.telegram_actions import AccountNotAuthenticatedError

    account_id = await _create_account(client)
    monkeypatch.setattr(
        "app.api.channel_hub.get_authorized_client",
        AsyncMock(side_effect=AccountNotAuthenticatedError("계정이 아직 인증되지 않았습니다.")),
    )

    res = await client.post(
        "/api/channel-hub/publish",
        json={"account_id": account_id, "channel_id": "-100111", "title": "공지"},
    )
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_publish_unknown_account_404s(client):
    res = await client.post(
        "/api/channel-hub/publish",
        json={"account_id": "does-not-exist", "channel_id": "-100111", "title": "공지"},
    )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_publish_requires_title(client):
    account_id = await _create_account(client)
    res = await client.post(
        "/api/channel-hub/publish",
        json={"account_id": account_id, "channel_id": "-100111", "title": ""},
    )
    assert res.status_code == 422
