from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api import chats as chats_api
from app.services import chat_actions


@pytest.mark.asyncio
async def test_send_message_endpoint_passes_media_type_to_chat_service(monkeypatch):
    async def fake_require_account_tenant_access(*args, **kwargs):
        return None

    async def fake_get_account(*args, **kwargs):
        return SimpleNamespace(id="acc-1")

    async def fake_send_chat_message(*args, **kwargs):
        return {"message_id": 42, "status": "sent"}

    monkeypatch.setattr(chats_api, "require_account_tenant_access", fake_require_account_tenant_access)
    monkeypatch.setattr(chats_api, "account_crud", SimpleNamespace(get_account=fake_get_account))
    monkeypatch.setattr(chats_api, "send_chat_message", fake_send_chat_message)

    result = await chats_api.send_message_endpoint(
        "acc-1",
        123,
        SimpleNamespace(text="hello", reply_to_msg_id=None, media_path="/tmp/file.jpg", media_type="photo"),
        identity=SimpleNamespace(user_id="user-1", tenant_id="tenant-1"),
        db=object(),
    )

    assert result == {"message_id": 42, "status": "sent"}


@pytest.mark.asyncio
async def test_send_chat_message_forwards_media_type_to_telethon(monkeypatch):
    class FakeClient:
        def __init__(self):
            self.calls = []

        async def send_file(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return SimpleNamespace(id=7)

        async def send_message(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return SimpleNamespace(id=8)

    fake_client = FakeClient()

    async def fake_get_authorized_client(account):
        return fake_client

    class FakeAsyncSession:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    def fake_async_session_maker():
        return FakeAsyncSession()

    async def fake_get_account(*args, **kwargs):
        return SimpleNamespace(id="acc-1")

    monkeypatch.setattr(chat_actions, "async_session_maker", fake_async_session_maker)
    monkeypatch.setattr(chat_actions, "account_crud", SimpleNamespace(get_account=fake_get_account))
    monkeypatch.setattr(chat_actions, "get_authorized_client", fake_get_authorized_client)

    await chat_actions.send_chat_message(
        "acc-1",
        123,
        "hello",
        media_path="/tmp/file.jpg",
        media_type="photo",
    )

    assert fake_client.calls[0][1]["file_type"] == "photo"
