from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import chats as chats_api
from app.api import deps as api_deps


def test_chat_media_upload_endpoint_accepts_file(monkeypatch, tmp_path):
    app = FastAPI()
    app.include_router(chats_api.router, prefix="/api/chat-telegram")

    async def fake_require_account_tenant_access(*args, **kwargs):
        return None

    async def fake_get_current_user(*args, **kwargs):
        return SimpleNamespace(user_id="user-1", tenant_id="tenant-1")

    async def fake_get_account(*args, **kwargs):
        return SimpleNamespace(id="acc-1")

    monkeypatch.setattr(chats_api, "require_account_tenant_access", fake_require_account_tenant_access)
    monkeypatch.setattr(chats_api, "get_current_user", fake_get_current_user)
    monkeypatch.setattr(chats_api, "account_crud", SimpleNamespace(get_account=fake_get_account))

    app.dependency_overrides[api_deps.get_current_user] = lambda: SimpleNamespace(user_id="user-1", tenant_id="tenant-1")
    app.dependency_overrides[api_deps.require_account_tenant_access] = lambda *args, **kwargs: None

    client = TestClient(app)
    response = client.post(
        "/api/chat-telegram/accounts/acc-1/upload",
        files={"file": ("test.png", b"fake-image-bytes", "image/png")},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["media_path"].endswith(".png")
