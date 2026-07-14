"""Official-channel membership verification gate for the free-trial signup flow.

Covers: membership accept/reject per Telegram status, fail-closed behavior when the
Telegram API is unavailable, that the frontend cannot fake membership, the full
start -> bot-link -> check -> verify-code -> 24h-trial -> API-key pipeline, single-use
token / duplicate-trial prevention, and that this whole gate is a no-op (existing
behavior preserved) when TELEGRAM_OFFICIAL_CHANNEL_ID isn't configured.
"""

from types import SimpleNamespace

import pytest

from app.config import settings
from app.core.security import hash_api_key
from app.crud import telegram_verification as verification_crud
from app.models.tenant import Tenant
from app.services.telegram_membership import MembershipCheckUnavailable, is_channel_member


# ── Helpers ──────────────────────────────────────────────────────────

class _FakeMember:
    def __init__(self, status: str):
        self.status = status


def _patch_channel_configured(monkeypatch):
    monkeypatch.setattr(settings, "telegram_official_channel_id", "@telemon_official")
    monkeypatch.setattr(settings, "telegram_bot_username", "telemon_bot")


def _patch_get_chat_member(monkeypatch, status_or_exc):
    """status_or_exc: a ChatMemberStatus-like string, or an Exception instance to raise."""

    async def fake_get_chat_member(self, chat_id, user_id):
        if isinstance(status_or_exc, Exception):
            raise status_or_exc
        return _FakeMember(status_or_exc)

    monkeypatch.setattr("app.services.telegram_membership.Bot.get_chat_member", fake_get_chat_member)
    monkeypatch.setattr(settings, "telegram_bot_token", "fake-token-for-tests")
    monkeypatch.setattr(settings, "telegram_official_channel_id", "@telemon_official")


async def _complete_signup_with_token(client, phone: str, monkeypatch, token: str) -> dict:
    captured: dict[str, str] = {}

    async def fake_send(phone: str, code: str) -> None:
        captured["code"] = code

    monkeypatch.setattr("app.api.auth.send_verification_sms", fake_send)

    send_res = await client.post("/api/auth/send-code", json={"phone": phone})
    assert send_res.status_code == 200

    return await client.post(
        "/api/auth/verify-code",
        json={"phone": phone, "code": captured["code"], "telegram_verification_token": token},
    )


# ── 1-4: membership status acceptance/rejection (unit-level, real Bot API mocked) ──


@pytest.mark.asyncio
async def test_member_status_passes_verification(monkeypatch):
    from telegram.constants import ChatMemberStatus

    _patch_get_chat_member(monkeypatch, ChatMemberStatus.MEMBER)
    assert await is_channel_member(123) is True


@pytest.mark.asyncio
async def test_administrator_status_passes_verification(monkeypatch):
    from telegram.constants import ChatMemberStatus

    _patch_get_chat_member(monkeypatch, ChatMemberStatus.ADMINISTRATOR)
    assert await is_channel_member(123) is True


@pytest.mark.asyncio
async def test_creator_owner_status_passes_verification(monkeypatch):
    from telegram.constants import ChatMemberStatus

    _patch_get_chat_member(monkeypatch, ChatMemberStatus.OWNER)
    assert await is_channel_member(123) is True


@pytest.mark.asyncio
async def test_non_member_left_status_is_rejected(monkeypatch):
    from telegram.constants import ChatMemberStatus

    _patch_get_chat_member(monkeypatch, ChatMemberStatus.LEFT)
    assert await is_channel_member(123) is False


@pytest.mark.asyncio
async def test_kicked_banned_status_is_rejected(monkeypatch):
    from telegram.constants import ChatMemberStatus

    _patch_get_chat_member(monkeypatch, ChatMemberStatus.BANNED)
    assert await is_channel_member(123) is False


@pytest.mark.asyncio
async def test_restricted_status_is_rejected(monkeypatch):
    """Not in the explicit accept-list (member/administrator/creator) — allow-list,
    not a deny-list, so anything else is rejected by default."""
    from telegram.constants import ChatMemberStatus

    _patch_get_chat_member(monkeypatch, ChatMemberStatus.RESTRICTED)
    assert await is_channel_member(123) is False


@pytest.mark.asyncio
async def test_telegram_api_failure_fails_closed(monkeypatch):
    from telegram.error import TelegramError

    _patch_get_chat_member(monkeypatch, TelegramError("network error"))
    with pytest.raises(MembershipCheckUnavailable):
        await is_channel_member(123)


@pytest.mark.asyncio
async def test_unconfigured_channel_fails_closed(monkeypatch):
    monkeypatch.setattr(settings, "telegram_bot_token", "")
    monkeypatch.setattr(settings, "telegram_official_channel_id", "")
    with pytest.raises(MembershipCheckUnavailable):
        await is_channel_member(123)


# ── 5: /check never trusts client-supplied membership state ─────────


@pytest.mark.asyncio
async def test_check_endpoint_ignores_client_claimed_status(client, db_session, monkeypatch):
    """The request schema has no status field at all, so there's nothing for a
    forged request to set — but also verify that even if bot /start was never
    received (server has no telegram_user_id for this token), the endpoint reports
    pending, never "verified", regardless of what the client sends."""
    _patch_channel_configured(monkeypatch)
    row = await verification_crud.create_verification(db_session)

    res = await client.post(
        "/api/telegram-verify/check",
        json={"token": row.id, "status": "verified", "verified": True},  # extra fields ignored
    )
    assert res.status_code == 200
    assert res.json()["status"] == "pending_bot_start"


@pytest.mark.asyncio
async def test_check_endpoint_rejects_unknown_token(client, monkeypatch):
    _patch_channel_configured(monkeypatch)
    res = await client.post("/api/telegram-verify/check", json={"token": "does-not-exist"})
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_check_endpoint_verifies_linked_member_server_side(client, db_session, monkeypatch):
    from telegram.constants import ChatMemberStatus

    _patch_get_chat_member(monkeypatch, ChatMemberStatus.MEMBER)
    row = await verification_crud.create_verification(db_session)
    await verification_crud.link_telegram_user(db_session, row.id, telegram_user_id=999)

    res = await client.post("/api/telegram-verify/check", json={"token": row.id})
    assert res.status_code == 200
    assert res.json()["status"] == "verified"

    await db_session.refresh(row)
    assert row.status == "verified"


@pytest.mark.asyncio
async def test_check_endpoint_unverified_when_not_a_member(client, db_session, monkeypatch):
    from telegram.constants import ChatMemberStatus

    _patch_get_chat_member(monkeypatch, ChatMemberStatus.LEFT)
    row = await verification_crud.create_verification(db_session)
    await verification_crud.link_telegram_user(db_session, row.id, telegram_user_id=999)

    res = await client.post("/api/telegram-verify/check", json={"token": row.id})
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "unverified"
    assert body["reason"] == "not_a_member"


@pytest.mark.asyncio
async def test_check_endpoint_fails_closed_when_telegram_unavailable(client, db_session, monkeypatch):
    from telegram.error import TelegramError

    _patch_get_chat_member(monkeypatch, TelegramError("boom"))
    row = await verification_crud.create_verification(db_session)
    await verification_crud.link_telegram_user(db_session, row.id, telegram_user_id=999)

    res = await client.post("/api/telegram-verify/check", json={"token": row.id})
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "unverified"
    assert body["reason"] == "membership_check_unavailable"


# ── 6-7: full pipeline into the existing trial/API-key flow ──────────


@pytest.mark.asyncio
async def test_verified_token_continues_into_existing_trial_and_api_key_flow(
    unauthenticated_client, db_session, monkeypatch
):
    monkeypatch.setattr(settings, "telegram_official_channel_id", "@telemon_official")

    row = await verification_crud.create_verification(db_session)
    await verification_crud.link_telegram_user(db_session, row.id, telegram_user_id=555)
    await verification_crud.mark_verified(db_session, row.id)

    res = await _complete_signup_with_token(
        unauthenticated_client, "+821099990001", monkeypatch, row.id
    )
    assert res.status_code == 200
    body = res.json()
    assert body["api_key"].startswith("sk-")

    from sqlalchemy import select
    tenant = (await db_session.execute(
        select(Tenant).where(Tenant.phone == "+821099990001")
    )).scalar_one()
    assert tenant.plan == "free"
    assert tenant.subscription_status == "active"
    assert tenant.trial_expires_at is not None

    from datetime import datetime, timedelta, timezone
    expected_min = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=23, minutes=59)
    assert expected_min <= tenant.trial_expires_at <= expected_min + timedelta(minutes=2)

    # API key issuance uses the existing secure (hash, shown-once) flow — the raw key
    # returned to the client must not equal what's stored server-side.
    from app.crud import user as user_crud
    user = await user_crud.get_user_by_phone(db_session, "+821099990001")
    assert user.api_key_hash == hash_api_key(body["api_key"])
    assert user.api_key_hash != body["api_key"]


@pytest.mark.asyncio
async def test_signup_rejected_without_valid_token_when_gate_configured(
    unauthenticated_client, db_session, monkeypatch
):
    monkeypatch.setattr(settings, "telegram_official_channel_id", "@telemon_official")

    res = await _complete_signup_with_token(
        unauthenticated_client, "+821099990002", monkeypatch, "not-a-real-token"
    )
    assert res.status_code == 403

    from sqlalchemy import select
    tenant = (await db_session.execute(
        select(Tenant).where(Tenant.phone == "+821099990002")
    )).scalar_one_or_none()
    assert tenant is None  # no half-created trial


@pytest.mark.asyncio
async def test_signup_rejected_when_token_never_verified(unauthenticated_client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "telegram_official_channel_id", "@telemon_official")

    row = await verification_crud.create_verification(db_session)
    await verification_crud.link_telegram_user(db_session, row.id, telegram_user_id=777)
    # Note: never marked verified.

    res = await _complete_signup_with_token(
        unauthenticated_client, "+821099990003", monkeypatch, row.id
    )
    assert res.status_code == 403


# ── 8: single-use token / duplicate trial prevention ─────────────────


@pytest.mark.asyncio
async def test_verified_token_cannot_be_reused_for_a_second_trial(
    unauthenticated_client, db_session, monkeypatch
):
    monkeypatch.setattr(settings, "telegram_official_channel_id", "@telemon_official")

    row = await verification_crud.create_verification(db_session)
    await verification_crud.link_telegram_user(db_session, row.id, telegram_user_id=888)
    await verification_crud.mark_verified(db_session, row.id)

    first = await _complete_signup_with_token(
        unauthenticated_client, "+821099990004", monkeypatch, row.id
    )
    assert first.status_code == 200

    second = await _complete_signup_with_token(
        unauthenticated_client, "+821099990005", monkeypatch, row.id
    )
    assert second.status_code == 403

    from sqlalchemy import select
    tenant_b = (await db_session.execute(
        select(Tenant).where(Tenant.phone == "+821099990005")
    )).scalar_one_or_none()
    assert tenant_b is None


@pytest.mark.asyncio
async def test_returning_user_does_not_need_a_token_and_gets_no_second_trial(
    unauthenticated_client, db_session, monkeypatch
):
    """Duplicate-trial prevention for the *same phone* already exists (Tenant.phone is
    the dedup key) — verify the channel-verification gate doesn't interfere with it,
    and that a returning user isn't newly blocked by this feature."""
    monkeypatch.setattr(settings, "telegram_official_channel_id", "@telemon_official")

    row = await verification_crud.create_verification(db_session)
    await verification_crud.link_telegram_user(db_session, row.id, telegram_user_id=111)
    await verification_crud.mark_verified(db_session, row.id)
    first = await _complete_signup_with_token(unauthenticated_client, "+821099990006", monkeypatch, row.id)
    assert first.status_code == 200
    first_key = first.json()["api_key"]

    # Same phone verifies again, with NO token this time — must succeed (no new trial
    # gate applies to an existing tenant) and must not create a second Tenant row.
    second = await _complete_signup_with_token(unauthenticated_client, "+821099990006", monkeypatch, None)
    assert second.status_code == 200
    assert second.json()["api_key"] != first_key  # existing behavior: key rotates

    from sqlalchemy import select, func
    count = (await db_session.execute(
        select(func.count()).select_from(Tenant).where(Tenant.phone == "+821099990006")
    )).scalar_one()
    assert count == 1


# ── 9: gate is a no-op when the feature isn't configured for this deployment ──


@pytest.mark.asyncio
async def test_signup_unaffected_when_channel_verification_not_configured(
    unauthenticated_client, db_session, monkeypatch
):
    monkeypatch.setattr(settings, "telegram_official_channel_id", "")

    res = await _complete_signup_with_token(
        unauthenticated_client, "+821099990007", monkeypatch, None
    )
    assert res.status_code == 200

    from sqlalchemy import select
    tenant = (await db_session.execute(
        select(Tenant).where(Tenant.phone == "+821099990007")
    )).scalar_one()
    assert tenant.plan == "free"
    assert tenant.trial_expires_at is not None


# ── 10: expired trial enforcement is untouched by this feature ───────


@pytest.mark.asyncio
async def test_expired_trial_enforcement_still_functions(db_session):
    from datetime import datetime, timedelta, timezone

    tenant = Tenant(
        phone="+821099990008",
        plan="free",
        subscription_status="active",
        trial_expires_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1),
    )
    db_session.add(tenant)
    await db_session.commit()
    await db_session.refresh(tenant)

    assert tenant.trial_expires_at is not None
    assert tenant.trial_expires_at < datetime.now(timezone.utc).replace(tzinfo=None)


# ── 11: paid users are unaffected ─────────────────────────────────────


@pytest.mark.asyncio
async def test_paid_plan_tenant_unaffected_by_channel_gate(unauthenticated_client, db_session, monkeypatch):
    from app.services.usage_tracker import apply_plan_limits

    monkeypatch.setattr(settings, "telegram_official_channel_id", "@telemon_official")

    tenant = Tenant(phone="+821099990009", subscription_status="active")
    db_session.add(tenant)
    await db_session.flush()
    await apply_plan_limits(db_session, tenant, "pro")

    # Returning (paid, already-existing tenant) — verify-code must succeed with no
    # channel-verification token at all, and must not touch their plan.
    res = await _complete_signup_with_token(unauthenticated_client, "+821099990009", monkeypatch, None)
    assert res.status_code == 200

    await db_session.refresh(tenant)
    assert tenant.plan == "pro"


# ── 12: a pending (unpaid/unverified) Tenant stub must not bypass the gate ────


@pytest.mark.asyncio
async def test_pending_tenant_stub_does_not_bypass_channel_gate(
    unauthenticated_client, db_session, monkeypatch
):
    """POST /api/payment/request-key is public and unauthenticated, and creates a
    "pending" Tenant row for any phone before any payment is confirmed. That stub
    must not be treated as an already-entitled tenant — verify-code must still
    demand real channel verification for it, exactly like a brand-new signup."""
    monkeypatch.setattr(settings, "telegram_official_channel_id", "@telemon_official")

    phone = "+821099990010"
    tenant = Tenant(phone=phone, plan="pro", subscription_status="pending", payment_ref="TM-TEST0001")
    db_session.add(tenant)
    await db_session.commit()

    res = await _complete_signup_with_token(unauthenticated_client, phone, monkeypatch, None)
    assert res.status_code == 403

    from app.crud import user as user_crud
    user = await user_crud.get_user_by_phone(db_session, phone)
    assert user is None  # no API key silently issued for the unverified pending stub

    await db_session.refresh(tenant)
    assert tenant.subscription_status == "pending"  # untouched


@pytest.mark.asyncio
async def test_pending_tenant_stub_succeeds_with_valid_token(
    unauthenticated_client, db_session, monkeypatch
):
    """A pending stub CAN still complete signup if it actually provides a real,
    server-verified channel-membership token — the gate isn't disabled, it just
    isn't skipped for free."""
    monkeypatch.setattr(settings, "telegram_official_channel_id", "@telemon_official")

    phone = "+821099990011"
    tenant = Tenant(phone=phone, plan="pro", subscription_status="pending", payment_ref="TM-TEST0002")
    db_session.add(tenant)
    await db_session.commit()

    row = await verification_crud.create_verification(db_session)
    await verification_crud.link_telegram_user(db_session, row.id, telegram_user_id=222)
    await verification_crud.mark_verified(db_session, row.id)

    res = await _complete_signup_with_token(unauthenticated_client, phone, monkeypatch, row.id)
    assert res.status_code == 200
    assert res.json()["api_key"].startswith("sk-")


# ── bot /start handler links the real Telegram identity ──────────────


@pytest.mark.asyncio
async def test_bot_start_handler_links_telegram_user_id(db_session, monkeypatch):
    import app.services.telegram_bot_service as bot_service

    class _FakeMessage:
        def __init__(self):
            self.replied = None

        async def reply_text(self, text):
            self.replied = text

    row = await verification_crud.create_verification(db_session)

    update = SimpleNamespace(
        message=_FakeMessage(),
        effective_user=SimpleNamespace(id=42424242),
    )
    context = SimpleNamespace(args=[row.id])

    monkeypatch.setattr(bot_service, "async_session_maker", lambda: _SessionCm(db_session))

    await bot_service.start_command(update, context)

    await db_session.refresh(row)
    assert row.telegram_user_id == 42424242
    assert row.status == "linked"
    assert "확인" in update.message.replied


class _SessionCm:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False
