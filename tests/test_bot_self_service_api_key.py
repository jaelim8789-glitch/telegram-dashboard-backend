"""Focused tests for the Telegram bot self-service API key flow.

Covers:
  - unauthorized/unlinked Telegram user
  - ineligible user (not a channel member)
  - eligible user issuance success
  - duplicate issuance prevention
  - concurrent/repeated button clicks (race-condition guard)
  - raw API key not logged
  - existing issuance flow regression (free_api_key / admin manual still work)
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app.config import settings
from app.core.security import generate_user_api_key, hash_api_key
from app.models.tenant import Tenant
from app.models.user import User
from app.services.bot_api_key_service import (
    _in_flight,
    _set_in_flight,
    handle_self_service_api_key,
)
from app.services.telegram_membership import MembershipCheckUnavailable


pytestmark = pytest.mark.asyncio


# ─── Helpers ────────────────────────────────────────────────────────────


def _patch_channel_config(monkeypatch):
    """Enable the channel-verification gate for tests."""
    monkeypatch.setattr(settings, "telegram_bot_token", "fake-token")
    monkeypatch.setattr(settings, "telegram_official_channel_id", "@TeleMon_2")
    monkeypatch.setattr(settings, "telegram_bot_username", "telemon_bot")


def _patch_membership(monkeypatch, is_member: bool = True):
    """Patch is_channel_member to return a fixed result."""

    async def fake_is_channel_member(telegram_user_id: int) -> bool:
        return is_member

    monkeypatch.setattr(
        "app.services.bot_api_key_service.is_channel_member",
        fake_is_channel_member,
    )


def _patch_membership_unavailable(monkeypatch):
    """Patch is_channel_member to raise MembershipCheckUnavailable."""

    async def fake_is_channel_member(telegram_user_id: int) -> bool:
        raise MembershipCheckUnavailable("test unavailable")

    monkeypatch.setattr(
        "app.services.bot_api_key_service.is_channel_member",
        fake_is_channel_member,
    )


async def _create_user_with_key(db_session, identifier: str, raw_key: str | None = None) -> User:
    if raw_key is None:
        raw_key = generate_user_api_key()
    user = User(phone=identifier, api_key_hash=hash_api_key(raw_key))
    db_session.add(user)
    await db_session.flush()
    await db_session.commit()
    return user


async def _create_pending_tenant(db_session, phone: str) -> Tenant:
    tenant = Tenant(
        phone=phone,
        plan="pro",
        subscription_status="pending",
        payment_ref="TM-TEST123",
    )
    db_session.add(tenant)
    await db_session.flush()
    await db_session.commit()
    return tenant


async def _create_expired_tenant(db_session, phone: str) -> Tenant:
    tenant = Tenant(
        phone=phone,
        plan="free",
        subscription_status="inactive",
        trial_expires_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1),
    )
    db_session.add(tenant)
    await db_session.flush()
    await db_session.commit()
    return tenant


# ─── 1. Unauthorized / unlinked Telegram user ──────────────────────────


async def test_unlinked_user_not_member_rejected(db_session, monkeypatch):
    """A Telegram user with no TeleMon account and no channel membership -> not_linked."""
    _patch_channel_config(monkeypatch)
    _patch_membership(monkeypatch, is_member=False)

    result = await handle_self_service_api_key(db_session, telegram_user_id=888001)

    assert result.status == "not_linked"
    assert result.api_key is None
    assert "TeleMon" in result.detail


async def test_unlinked_user_but_member_gets_issued(db_session, monkeypatch):
    """A new Telegram user who IS a channel member -> gets a free-trial key."""
    _patch_channel_config(monkeypatch)
    _patch_membership(monkeypatch, is_member=True)

    result = await handle_self_service_api_key(db_session, telegram_user_id=888002)

    assert result.status == "issued"
    assert result.api_key is not None
    assert result.api_key.startswith("sk-")

    from sqlalchemy import select
    user = (
        await db_session.execute(select(User).where(User.phone == "tg_888002"))
    ).scalar_one_or_none()
    assert user is not None
    assert user.api_key_hash == hash_api_key(result.api_key)


# ─── 2. Ineligible user ────────────────────────────────────────────────


async def test_ineligible_user_not_member_rejected(db_session, monkeypatch):
    """Existing user but not a channel member -> not_eligible."""
    _patch_channel_config(monkeypatch)
    _patch_membership(monkeypatch, is_member=False)

    user = User(phone="tg_888003")
    db_session.add(user)
    await db_session.flush()
    await db_session.commit()

    result = await handle_self_service_api_key(db_session, telegram_user_id=888003)

    assert result.status == "not_eligible"
    assert result.api_key is None


async def test_ineligible_expired_trial_rejected(db_session, monkeypatch):
    """User with an expired trial and no active subscription -> not_eligible."""
    _patch_channel_config(monkeypatch)
    _patch_membership(monkeypatch, is_member=True)

    identifier = "tg_888004"
    user = User(phone=identifier)
    db_session.add(user)
    await db_session.flush()
    await db_session.commit()

    await _create_expired_tenant(db_session, identifier)

    result = await handle_self_service_api_key(db_session, telegram_user_id=888004)

    assert result.status == "not_eligible"
    assert result.api_key is None


# ─── 3. Eligible user issuance success ─────────────────────────────────


async def test_eligible_user_issuance_success(db_session, monkeypatch):
    """A channel member with no existing key -> key issued, hash stored, tenant created."""
    _patch_channel_config(monkeypatch)
    _patch_membership(monkeypatch, is_member=True)

    result = await handle_self_service_api_key(db_session, telegram_user_id=888005)

    assert result.status == "issued"
    assert result.api_key is not None
    assert result.api_key.startswith("sk-")

    from sqlalchemy import select
    user = (
        await db_session.execute(select(User).where(User.phone == "tg_888005"))
    ).scalar_one_or_none()
    assert user is not None
    assert user.api_key_hash == hash_api_key(result.api_key)
    assert user.api_key_hash != result.api_key

    tenant = (
        await db_session.execute(select(Tenant).where(Tenant.phone == "tg_888005"))
    ).scalar_one_or_none()
    assert tenant is not None
    assert tenant.plan == "free"
    assert tenant.subscription_status == "active"


# ─── 4. Duplicate issuance prevention ──────────────────────────────────


async def test_duplicate_issuance_prevented(db_session, monkeypatch):
    """User who already has a key -> already_issued, no new key, no raw key exposed."""
    _patch_channel_config(monkeypatch)
    _patch_membership(monkeypatch, is_member=True)

    raw_key = generate_user_api_key()
    await _create_user_with_key(db_session, "tg_888006", raw_key)

    result = await handle_self_service_api_key(db_session, telegram_user_id=888006)

    assert result.status == "already_issued"
    assert result.api_key is None
    assert raw_key not in result.detail


# ─── 5. Concurrent / repeated button clicks (race-condition guard) ────


async def test_concurrent_clicks_race_condition_guard(db_session, monkeypatch):
    """Two simultaneous requests from the same user -> only one succeeds."""
    _patch_channel_config(monkeypatch)
    _patch_membership(monkeypatch, is_member=True)

    _set_in_flight(888007, True)

    try:
        result = await handle_self_service_api_key(db_session, telegram_user_id=888007)
        assert result.status == "server_error"
        assert "처리 중" in result.detail
    finally:
        _set_in_flight(888007, False)


async def test_repeated_clicks_after_completion_ok(db_session, monkeypatch):
    """After the first request completes, a second click returns already_issued."""
    _patch_channel_config(monkeypatch)
    _patch_membership(monkeypatch, is_member=True)

    result1 = await handle_self_service_api_key(db_session, telegram_user_id=888008)
    assert result1.status == "issued"

    result2 = await handle_self_service_api_key(db_session, telegram_user_id=888008)
    assert result2.status == "already_issued"
    assert result2.api_key is None


async def test_concurrent_issuance_only_one_key(db_session, monkeypatch):
    """Genuinely concurrent asyncio tasks -> only one issues."""
    _patch_channel_config(monkeypatch)
    _patch_membership(monkeypatch, is_member=True)

    _in_flight.clear()

    results = await asyncio.gather(
        handle_self_service_api_key(db_session, telegram_user_id=888009),
        handle_self_service_api_key(db_session, telegram_user_id=888009),
    )

    statuses = [r.status for r in results]
    assert "issued" in statuses
    assert statuses.count("issued") == 1


# ─── 6. Raw API key not logged ─────────────────────────────────────────


async def test_raw_api_key_not_logged(db_session, monkeypatch, caplog):
    """The raw API key must never appear in log output."""
    _patch_channel_config(monkeypatch)
    _patch_membership(monkeypatch, is_member=True)

    with caplog.at_level(logging.INFO, logger="app.services.bot_api_key_service"):
        result = await handle_self_service_api_key(db_session, telegram_user_id=888010)

    assert result.status == "issued"
    raw_key = result.api_key
    assert raw_key is not None

    for record in caplog.records:
        assert raw_key not in record.getMessage()


async def test_raw_api_key_not_logged_on_duplicate(db_session, monkeypatch, caplog):
    """The raw key must not leak in logs even on the already_issued path."""
    _patch_channel_config(monkeypatch)
    _patch_membership(monkeypatch, is_member=True)

    raw_key = generate_user_api_key()
    await _create_user_with_key(db_session, "tg_888011", raw_key)

    with caplog.at_level(logging.INFO, logger="app.services.bot_api_key_service"):
        result = await handle_self_service_api_key(db_session, telegram_user_id=888011)

    assert result.status == "already_issued"
    for record in caplog.records:
        assert raw_key not in record.getMessage()


# ─── 7. Payment pending ────────────────────────────────────────────────


async def test_payment_pending_rejected(db_session, monkeypatch):
    """User with a pending USDT payment -> payment_pending, no key issued."""
    _patch_channel_config(monkeypatch)
    _patch_membership(monkeypatch, is_member=True)

    identifier = "tg_888012"
    user = User(phone=identifier)
    db_session.add(user)
    await db_session.flush()
    await db_session.commit()

    await _create_pending_tenant(db_session, identifier)

    result = await handle_self_service_api_key(db_session, telegram_user_id=888012)

    assert result.status == "payment_pending"
    assert result.api_key is None


# ─── 8. Server error (membership check unavailable) ───────────────────


async def test_server_error_on_membership_unavailable(db_session, monkeypatch):
    """If the Telegram API is unreachable -> server_error (fail closed)."""
    _patch_channel_config(monkeypatch)
    _patch_membership_unavailable(monkeypatch)

    result = await handle_self_service_api_key(db_session, telegram_user_id=888013)

    assert result.status == "server_error"
    assert result.api_key is None


# ─── 9. Existing issuance flow regression ──────────────────────────────


async def test_existing_free_api_key_flow_still_works(client, db_session, monkeypatch):
    """Regression: the existing /api/free-api-key/issue endpoint must still work."""
    _patch_channel_config(monkeypatch)

    async def fake_get_chat_member(self, chat_id, user_id):
        m = MagicMock()
        from telegram.constants import ChatMemberStatus
        m.status = ChatMemberStatus.MEMBER
        return m

    monkeypatch.setattr(
        "app.services.telegram_membership.Bot.get_chat_member",
        fake_get_chat_member,
    )

    from app.crud import telegram_verification as verification_crud

    row = await verification_crud.create_verification(db_session)
    await verification_crud.link_telegram_user(db_session, row.id, 999001)
    await verification_crud.mark_verified(db_session, row)

    res = await client.post(
        "/api/free-api-key/issue",
        json={"token": row.id, "phone": "+821099990001"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["api_key"].startswith("sk-")
    assert body["already_issued"] is False


async def test_existing_admin_manual_issue_still_works(client, db_session):
    """Regression: the admin manual-issue endpoint must still work."""
    from app.core.security import create_access_token
    from app.database import get_db
    from app.main import app

    async def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db

    phone = "+821099990002"
    token = create_access_token()
    res = await client.post(
        "/api/admin/manual-issue-key",
        json={"user_identifier": phone},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    assert res.json()["api_key"].startswith("sk-")
    assert res.json()["already_issued"] is False

    app.dependency_overrides.clear()


# ─── 10. Idempotency ──────────────────────────────────────────────────


async def test_idempotent_across_sessions(db_session, monkeypatch):
    """After issuance, a new call should return already_issued."""
    _patch_channel_config(monkeypatch)
    _patch_membership(monkeypatch, is_member=True)

    result1 = await handle_self_service_api_key(db_session, telegram_user_id=888014)
    assert result1.status == "issued"

    _in_flight.clear()
    result2 = await handle_self_service_api_key(db_session, telegram_user_id=888014)
    assert result2.status == "already_issued"
    assert result2.api_key is None


# ─── 11. Cross-user isolation ──────────────────────────────────────────


async def test_cross_user_isolation(db_session, monkeypatch):
    """User A's key must never be returned to User B."""
    _patch_channel_config(monkeypatch)
    _patch_membership(monkeypatch, is_member=True)

    result_a = await handle_self_service_api_key(db_session, telegram_user_id=888020)
    assert result_a.status == "issued"
    key_a = result_a.api_key

    result_b = await handle_self_service_api_key(db_session, telegram_user_id=888021)
    assert result_b.status == "issued"
    key_b = result_b.api_key

    assert key_a != key_b

    result_b2 = await handle_self_service_api_key(db_session, telegram_user_id=888021)
    assert result_b2.status == "already_issued"
    assert result_b2.api_key is None
    assert key_a not in (result_b2.detail or "")