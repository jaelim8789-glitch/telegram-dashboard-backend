import asyncio
from types import SimpleNamespace

from fastapi import UploadFile

from app.api import chats as chats_api


def test_chat_media_upload_endpoint_accepts_file(monkeypatch):
    async def fake_require_account_tenant_access(*args, **kwargs):
        return None

    async def fake_get_account(*args, **kwargs):
        return SimpleNamespace(id="acc-1")

    async def fake_save_broadcast_media(upload):
        return f"/tmp/{upload.filename}"

    monkeypatch.setattr(chats_api, "require_account_tenant_access", fake_require_account_tenant_access)
    monkeypatch.setattr(chats_api, "account_crud", SimpleNamespace(get_account=fake_get_account))
    monkeypatch.setattr(chats_api, "save_broadcast_media", fake_save_broadcast_media)
    monkeypatch.setattr(chats_api, "infer_media_type", lambda filename: "photo")

    upload = UploadFile(
        filename="test.png",
        file=__import__("io").BytesIO(b"fake-image-bytes"),
        headers={"content-type": "image/png"},
    )

    payload = asyncio.run(
        chats_api.upload_attachment_endpoint(
            "acc-1",
            upload,
            identity=SimpleNamespace(user_id="user-1", tenant_id="tenant-1"),
            db=object(),
        )
    )

    assert payload["success"] is True
    assert payload["media_path"].endswith("test.png")
