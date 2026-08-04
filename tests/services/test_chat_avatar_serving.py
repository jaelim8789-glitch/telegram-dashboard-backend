"""Verify get_chat_details() actually persists the downloaded profile photo to
disk and returns a photo_url that resolves — via the real avatar-serving route,
not just a string that happens to look right — to that exact file.

Regression context: get_chat_details() previously called
client.download_profile_photo(entity, file=bytes) and immediately discarded the
result, only ever setting a boolean has_photo flag. The frontend's "tap avatar
to zoom" feature needs a real, servable photo_url.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon.tl.types import User

from app.api import chats as chats_api
from app.services import media as media_service
from app.services.chat_actions import get_chat_details

FAKE_PHOTO_BYTES = b"\xff\xd8\xff\xe0fake-jpeg-bytes-not-a-real-image"


def _make_user(user_id: int) -> User:
    return User(
        id=user_id, access_hash=111, first_name="Ada",
        is_self=False, contact=False, mutual_contact=False, deleted=False,
        bot=False, bot_chat_history=False, bot_nochats=False, verified=False,
        restricted=False, min=False, bot_inline_geo=False, support=False,
        scam=False, apply_min_photo=False, fake=False, bot_attach_menu=False,
        premium=False, attach_menu_enabled=False, bot_can_edit=False,
        close_friend=False, stories_hidden=False, stories_unavailable=False,
        contact_require_premium=False, bot_business=False,
    )


@pytest.fixture(autouse=True)
def _isolated_avatar_root(tmp_path, monkeypatch):
    """Point AVATAR_ROOT at a throwaway directory so the test never touches
    (or depends on) the real ./media/avatars tree, and never leaves files
    behind across runs."""
    isolated_root = tmp_path / "avatars"
    isolated_root.mkdir()
    monkeypatch.setattr(media_service, "AVATAR_ROOT", isolated_root)
    return isolated_root


@pytest.mark.asyncio
async def test_get_chat_details_saves_photo_and_returns_working_url(_isolated_avatar_root):
    account_id = "acc-42"
    chat_id = 987654321

    client = AsyncMock()
    client.get_entity = AsyncMock(return_value=_make_user(user_id=chat_id))
    client.download_profile_photo = AsyncMock(return_value=FAKE_PHOTO_BYTES)

    info = await get_chat_details(client, chat_id, account_id=account_id)

    # 1. has_photo / photo_url are present and shaped correctly.
    assert info["has_photo"] is True
    assert "photo_url" in info
    photo_url = info["photo_url"]

    # 2. A real file was written, at the exact path avatar_file_path() computes
    #    for this account_id/chat_id — not just "some file somewhere".
    expected_path = media_service.avatar_file_path(account_id, chat_id)
    assert expected_path.exists()
    assert expected_path.read_bytes() == FAKE_PHOTO_BYTES
    assert expected_path.parent == _isolated_avatar_root / account_id

    # 3. The returned photo_url is the exact route this codebase uses to serve
    #    it (app.api.chats router prefix + account/chat path), and hitting that
    #    route resolves back to the file we just verified was written above —
    #    not merely a string that "looks like" a URL.
    assert photo_url == f"/api/chat-telegram/accounts/{account_id}/dialogs/{chat_id}/avatar"

    async def fake_require_account_tenant_access(*args, **kwargs):
        return None

    async def fake_get_account(*args, **kwargs):
        return SimpleNamespace(id=account_id)

    import pytest as _pytest
    monkeypatch = _pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(chats_api, "require_account_tenant_access", fake_require_account_tenant_access)
        monkeypatch.setattr(chats_api, "account_crud", SimpleNamespace(get_account=fake_get_account))

        response = await chats_api.get_chat_avatar(
            account_id,
            str(chat_id),
            db=object(),
            identity=SimpleNamespace(user_id="user-1", tenant_id="tenant-1"),
        )
    finally:
        monkeypatch.undo()

    # FileResponse.path is the filesystem path Starlette will actually stream
    # back for this route — confirm it is the SAME file get_chat_details() wrote.
    assert str(response.path) == str(expected_path)


@pytest.mark.asyncio
async def test_get_chat_details_skips_redownload_when_cache_is_fresh(_isolated_avatar_root):
    account_id = "acc-42"
    chat_id = 555

    path = media_service.avatar_file_path(account_id, chat_id)
    path.write_bytes(b"already-cached-bytes")

    client = AsyncMock()
    client.get_entity = AsyncMock(return_value=_make_user(user_id=chat_id))
    client.download_profile_photo = AsyncMock(return_value=FAKE_PHOTO_BYTES)

    info = await get_chat_details(client, chat_id, account_id=account_id)

    # Fresh cache hit: no re-download, existing bytes on disk untouched.
    client.download_profile_photo.assert_not_awaited()
    assert info["has_photo"] is True
    assert path.read_bytes() == b"already-cached-bytes"


@pytest.mark.asyncio
async def test_avatar_path_rejects_path_traversal():
    with pytest.raises(ValueError):
        media_service.avatar_file_path("acc-1", "../../etc/passwd")
    with pytest.raises(ValueError):
        media_service.avatar_file_path("../evil", "123")
