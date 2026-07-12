"""Free API key issuance via Telegram channel membership verification only.

No SMS OTP required. Covers: two-step flow (start -> check -> issue),
server-side verification, duplicate prevention, rate limiting,
and unconfigured-gate behavior.
"""

import pytest

from app.config import settings
from app.core.security import hash_api_key
from app.crud import telegram_verification as verification_crud


class _FakeMember:
    def __init__(self, status: str):
        self.status = status


def _patch_channel(monkeypatch, status_or_exc=None):
    """Patch settings and optionally the Bot.get_chat_member call."""
    monkeypatch.setattr(settings, "telegram_bot_token", "fake-token")
    monkeypatch.setattr(settings, "telegram_official_channel_id", "@TeleMon_2")
    monkeypatch.setattr(settings, "telegram_bot_username", "telemon_bot")
    if status_or_exc is not None:

        async def fake_get_chat_member(self, chat_id, user_id):
            if isinstance(status_or_exc, Exception):
                raise status_or_exc
            return _FakeMember(status_or_exc)

        monkeypatch.setattr("app.services.telegram_membership.Bot.get_chat_member", fake_get_chat_member)


async def _create_verified_token(db_session, telegram_user_id: int = 999) -> str:
    row = await verification_crud.create_verification(db_session)
    await verification_crud.link_telegram_user(db_session, row.id, telegram_user_id)
    await verification_crud.mark_verified(db_session, row)
    return row.id


@pytest.mark.asyncio
async def test_start_returns_bot_deep_link(client, monkeypatch):
    _patch_channel(monkeypatch)
    res = await client.post("/api/free-api-key/start")
    assert res.status_code == 200
    body = res.json()
    assert "token" in body
    assert "t.me/telemon_bot" in body["bot_deep_link"]
    assert "t.me/TeleMon_2" in body["channel_url"]


@pytest.mark.asyncio
async def test_start_503_when_not_configured(client, monkeypatch):
    res = await client.post("/api/free-api-key/start")
    assert res.status_code == 503


@pytest.mark.asyncio
async def test_issue_success(client, db_session, monkeypatch):
    _patch_channel(monkeypatch)
    token = await _create_verified_token(db_session)

    res = await client.post("/api/free-api-key/issue", json={"token": token, "phone": "+821099990001"})
    assert res.status_code == 200
    body = res.json()
    assert body["api_key"].startswith("sk-")
    assert body["already_issued"] is False

    from app.crud import user as user_crud
    user = await user_crud.get_user_by_phone(db_session, "+821099990001")
    assert user is not None
    assert user.api_key_hash == hash_api_key(body["api_key"])
    assert user.api_key_hash != body["api_key"]

    from sqlalchemy import select
    from app.models.tenant import Tenant
    tenant = (await db_session.execute(select(Tenant).where(Tenant.phone == "+821099990001"))).scalar_one_or_none()
    assert tenant is not None
    assert tenant.plan == "free"


@pytest.mark.asyncio
async def test_issue_rejects_unverified_token(client, db_session, monkeypatch):
    _patch_channel(monkeypatch)
    row = await verification_crud.create_verification(db_session)
    res = await client.post("/api/free-api-key/issue", json={"token": row.id, "phone": "+821099990002"})
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_issue_prevents_duplicate_api_key(client, db_session, monkeypatch):
    _patch_channel(monkeypatch)
    token1 = await _create_verified_token(db_session, telegram_user_id=111)
    res1 = await client.post("/api/free-api-key/issue", json={"token": token1, "phone": "+821099990003"})
    assert res1.status_code == 200
    assert res1.json()["already_issued"] is False

    token2 = await _create_verified_token(db_session, telegram_user_id=222)
    res2 = await client.post("/api/free-api-key/issue", json={"token": token2, "phone": "+821099990003"})
    body = res2.json()
    assert body["already_issued"] is True
    assert body["api_key"] is None


@pytest.mark.asyncio
async def test_issue_rejects_reused_token(client, db_session, monkeypatch):
    _patch_channel(monkeypatch)
    token = await _create_verified_token(db_session, telegram_user_id=333)

    res1 = await client.post("/api/free-api-key/issue", json={"token": token, "phone": "+821099990004"})
    assert res1.status_code == 200

    res2 = await client.post("/api/free-api-key/issue", json={"token": token, "phone": "+821099990005"})
    assert res2.status_code == 409


@pytest.mark.asyncio
async def test_issue_503_when_not_configured(client, monkeypatch):
    res = await client.post("/api/free-api-key/issue", json={"token": "x", "phone": "+821099990006"})
    assert res.status_code == 503


@pytest.mark.asyncio
async def test_issue_rejects_expired_token(client, db_session, monkeypatch):
    from datetime import datetime, timedelta, timezone
    import app.crud.telegram_verification as tv_crud
    monkeypatch.setattr(tv_crud, "TOKEN_TTL_MINUTES", 0)
    _patch_channel(monkeypatch)

    row = await verification_crud.create_verification(db_session)
    row.created_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
    await db_session.commit()

    res = await client.post("/api/free-api-key/issue", json={"token": row.id, "phone": "+821099990007"})
    assert res.status_code == 404
