import io

import pytest
from fastapi import HTTPException, UploadFile

from app.core.crypto import decrypt_session, encrypt_session
from app.services.media import save_broadcast_media


def test_encrypt_decrypt_roundtrip():
    ciphertext = encrypt_session("my-telethon-session-string")
    assert ciphertext != "my-telethon-session-string"
    assert decrypt_session(ciphertext) == "my-telethon-session-string"


def test_decrypt_invalid_token_raises_value_error():
    with pytest.raises(ValueError):
        decrypt_session("not-a-valid-fernet-token")


@pytest.mark.asyncio
async def test_save_broadcast_media_rejects_non_image_content_type():
    upload = UploadFile(filename="virus.exe", file=io.BytesIO(b"data"), headers={"content-type": "application/x-msdownload"})
    with pytest.raises(HTTPException) as exc_info:
        await save_broadcast_media(upload)
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_save_broadcast_media_rejects_oversized_file(monkeypatch):
    monkeypatch.setattr("app.services.media.MAX_MEDIA_SIZE_BYTES", 10)
    upload = UploadFile(filename="big.jpg", file=io.BytesIO(b"x" * 100), headers={"content-type": "image/jpeg"})
    with pytest.raises(HTTPException) as exc_info:
        await save_broadcast_media(upload)
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_save_broadcast_media_success_uses_server_generated_filename():
    upload = UploadFile(filename="../../etc/passwd.jpg", file=io.BytesIO(b"fake-image-bytes"), headers={"content-type": "image/jpeg"})
    saved_path = await save_broadcast_media(upload)
    try:
        # The client-supplied filename (including any path traversal attempt) must never
        # end up in the saved path — only a server-generated uuid + known extension.
        assert "passwd" not in saved_path
        assert ".." not in saved_path
        assert saved_path.endswith(".jpg")
    finally:
        import os

        if os.path.exists(saved_path):
            os.remove(saved_path)
