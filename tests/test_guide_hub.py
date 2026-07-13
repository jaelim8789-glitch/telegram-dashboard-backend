"""Tests for the 이용 가이드 허브 (guide hub) feature:
  - keyboard build omits buttons with no configured URL, pairs 2-per-row
  - first publish sends + pins a new message and persists chat/message id
  - second publish edits the same message in place (no duplicate row)
  - edit failure (e.g. message deleted out-of-band) falls back to a fresh post
  - unconfigured bot token fails closed with GuideHubUnavailable
  - admin-only: the HTTP endpoint rejects unauthenticated callers
"""

import pytest

from app.config import settings
from app.core.security import create_access_token
from app.crud import guide_hub as guide_hub_crud
from app.services.guide_hub_service import (
    GUIDE_HUB_BUTTONS,
    GuideHubUnavailable,
    _build_keyboard,
    publish_or_update_guide_hub,
)

def _patch_links(monkeypatch, links: dict[str, str]):
    import json

    monkeypatch.setattr(settings, "telegram_guide_hub_links_json", json.dumps(links))


def _patch_bot_config(monkeypatch):
    monkeypatch.setattr(settings, "telegram_bot_token", "fake-token")
    monkeypatch.setattr(settings, "telegram_official_channel_id", "@TeleMon_2")


class _FakeMessage:
    def __init__(self, message_id: int):
        self.message_id = message_id


class _FakeBot:
    """Records calls instead of hitting the real Telegram API."""

    def __init__(self, token: str):
        self.token = token
        self.sent: list[dict] = []
        self.edited: list[dict] = []
        self.pinned: list[dict] = []
        self.next_message_id = 555
        self.edit_should_fail = False

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return _FakeMessage(self.next_message_id)

    async def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        if self.edit_should_fail:
            from telegram.error import TelegramError

            raise TelegramError("message to edit not found")
        self.edited.append({"chat_id": chat_id, "message_id": message_id, "text": text})

    async def pin_chat_message(self, chat_id, message_id, disable_notification=False):
        self.pinned.append({"chat_id": chat_id, "message_id": message_id})


# ── keyboard construction ────────────────────────────────────────────────


def test_keyboard_omits_unconfigured_buttons(monkeypatch):
    _patch_links(monkeypatch, {"free_trial": "https://t.me/TeleMon_2/10"})
    markup = _build_keyboard()
    all_buttons = [b for row in markup.inline_keyboard for b in row]
    assert len(all_buttons) == 1
    assert all_buttons[0].url == "https://t.me/TeleMon_2/10"


def test_keyboard_pairs_two_per_row(monkeypatch):
    links = {key: f"https://t.me/TeleMon_2/{i}" for i, (key, _label) in enumerate(GUIDE_HUB_BUTTONS)}
    _patch_links(monkeypatch, links)
    markup = _build_keyboard()
    assert len(markup.inline_keyboard) == len(GUIDE_HUB_BUTTONS) // 2
    assert all(len(row) == 2 for row in markup.inline_keyboard)


def test_keyboard_empty_when_no_links_configured(monkeypatch):
    _patch_links(monkeypatch, {})
    markup = _build_keyboard()
    assert len(markup.inline_keyboard) == 0


# ── publish / update ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_publish_fails_closed_without_bot_token(db_session, monkeypatch):
    monkeypatch.setattr(settings, "telegram_bot_token", "")
    with pytest.raises(GuideHubUnavailable):
        await publish_or_update_guide_hub(db_session)


@pytest.mark.asyncio
async def test_first_publish_sends_and_pins(db_session, monkeypatch):
    _patch_bot_config(monkeypatch)
    _patch_links(monkeypatch, {"free_trial": "https://t.me/TeleMon_2/10"})
    fake_bot = _FakeBot("fake-token")
    monkeypatch.setattr("app.services.guide_hub_service.Bot", lambda token: fake_bot)

    chat_id, message_id, created = await publish_or_update_guide_hub(db_session)

    assert created is True
    assert message_id == fake_bot.next_message_id
    assert len(fake_bot.sent) == 1
    assert len(fake_bot.pinned) == 1

    row = await guide_hub_crud.get_latest(db_session)
    assert row is not None
    assert row.message_id == message_id
    assert row.chat_id == chat_id


@pytest.mark.asyncio
async def test_second_publish_edits_existing_message(db_session, monkeypatch):
    _patch_bot_config(monkeypatch)
    _patch_links(monkeypatch, {"free_trial": "https://t.me/TeleMon_2/10"})
    fake_bot = _FakeBot("fake-token")
    monkeypatch.setattr("app.services.guide_hub_service.Bot", lambda token: fake_bot)

    await publish_or_update_guide_hub(db_session)
    chat_id, message_id, created = await publish_or_update_guide_hub(db_session)

    assert created is False
    assert len(fake_bot.sent) == 1  # no second send
    assert len(fake_bot.edited) == 1  # edited instead
    assert len(fake_bot.pinned) == 1  # not re-pinned

    row = await guide_hub_crud.get_latest(db_session)
    assert row is not None
    assert row.message_id == fake_bot.next_message_id


@pytest.mark.asyncio
async def test_edit_failure_falls_back_to_new_post(db_session, monkeypatch):
    _patch_bot_config(monkeypatch)
    _patch_links(monkeypatch, {"free_trial": "https://t.me/TeleMon_2/10"})
    fake_bot = _FakeBot("fake-token")
    monkeypatch.setattr("app.services.guide_hub_service.Bot", lambda token: fake_bot)

    await publish_or_update_guide_hub(db_session)
    fake_bot.edit_should_fail = True
    fake_bot.next_message_id = 777

    chat_id, message_id, created = await publish_or_update_guide_hub(db_session)

    assert created is True
    assert message_id == 777
    assert len(fake_bot.sent) == 2
    assert len(fake_bot.pinned) == 2


# ── admin-only HTTP endpoint ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_publish_endpoint_rejects_unauthenticated(unauthenticated_client):
    res = await unauthenticated_client.post("/api/admin/guide-hub/publish")
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_publish_endpoint_returns_503_when_unconfigured(unauthenticated_client, db_session, monkeypatch):
    from app.main import app
    import app.database as db_mod
    from app.api.admin import get_db as admin_get_db

    async def _override():
        yield db_session

    app.dependency_overrides[db_mod.get_db] = _override
    app.dependency_overrides[admin_get_db] = _override
    monkeypatch.setattr(settings, "telegram_bot_token", "")
    try:
        token = create_access_token()
        res = await unauthenticated_client.post(
            "/api/admin/guide-hub/publish",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert res.status_code == 503
    finally:
        app.dependency_overrides.pop(db_mod.get_db, None)
        app.dependency_overrides.pop(admin_get_db, None)
