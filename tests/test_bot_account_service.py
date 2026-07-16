"""Tests for the Telegram bot's account/billing self-service layer
(app/services/bot_account_service.py) — plan/account snapshot, USDT purchase
start, renew, payment check/claim, and purchase history. Mirrors the pattern
in tests/test_bot_self_service_api_key.py: call the service functions
directly with a test DB session, no Update/CallbackQuery mocking.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.core.security import generate_user_api_key, hash_api_key
from app.models.api_key import APIKey
from app.models.tenant import PaymentRecord, Tenant
from app.models.user import User
from app.services import bot_account_service

pytestmark = pytest.mark.asyncio


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ─── get_account_snapshot ───────────────────────────────────────────────


async def test_snapshot_unlinked_user(db_session):
    snapshot = await bot_account_service.get_account_snapshot(db_session, telegram_user_id=700001)
    assert snapshot.linked is False
    assert snapshot.plan is None


async def test_snapshot_linked_active_trial(db_session):
    identifier = "tg_700002"
    db_session.add(User(phone=identifier))
    db_session.add(
        Tenant(
            phone=identifier,
            plan="free",
            subscription_status="active",
            trial_expires_at=_utcnow_naive() + timedelta(hours=24),
        )
    )
    await db_session.commit()

    snapshot = await bot_account_service.get_account_snapshot(db_session, telegram_user_id=700002)
    assert snapshot.linked is True
    assert snapshot.plan == "free"
    assert snapshot.plan_name == "Free"
    assert snapshot.subscription_status == "active"
    assert snapshot.trial_expires_at is not None
    assert snapshot.has_api_key is False


async def test_snapshot_reports_api_key_issued(db_session):
    identifier = "tg_700003"
    raw_key = generate_user_api_key()
    db_session.add(User(phone=identifier, api_key_hash=hash_api_key(raw_key)))
    db_session.add(Tenant(phone=identifier, plan="pro", subscription_status="active"))
    await db_session.commit()

    snapshot = await bot_account_service.get_account_snapshot(db_session, telegram_user_id=700003)
    assert snapshot.has_api_key is True
    assert snapshot.plan_name == "Pro"


# ─── start_purchase ──────────────────────────────────────────────────────


async def test_start_purchase_invalid_plan(db_session):
    result = await bot_account_service.start_purchase(db_session, telegram_user_id=700010, plan="bogus")
    assert result.status == "invalid_plan"


async def test_start_purchase_rejects_free_plan(db_session):
    result = await bot_account_service.start_purchase(db_session, telegram_user_id=700011, plan="free")
    assert result.status == "invalid_plan"


async def test_start_purchase_creates_pending_tenant_and_user(db_session):
    result = await bot_account_service.start_purchase(db_session, telegram_user_id=700012, plan="pro")

    assert result.status == "ok"
    assert result.plan == "pro"
    assert result.payment_ref is not None
    assert result.amount_usdt == 100

    identifier = "tg_700012"
    tenant = (
        await db_session.execute(select(Tenant).where(Tenant.phone == identifier))
    ).scalar_one_or_none()
    assert tenant is not None
    assert tenant.plan == "pro"
    assert tenant.subscription_status == "pending"
    assert tenant.payment_ref == result.payment_ref

    user = (await db_session.execute(select(User).where(User.phone == identifier))).scalar_one_or_none()
    assert user is not None, "a User must be created so the tg-only payer has a recoverable identity"


async def test_start_purchase_conflicts_on_already_active(db_session):
    identifier = "tg_700013"
    db_session.add(Tenant(phone=identifier, plan="pro", subscription_status="active"))
    await db_session.commit()

    result = await bot_account_service.start_purchase(db_session, telegram_user_id=700013, plan="team")
    assert result.status == "already_active"


# ─── start_renew ─────────────────────────────────────────────────────────


async def test_renew_without_prior_plan(db_session):
    result = await bot_account_service.start_renew(db_session, telegram_user_id=700020)
    assert result.status == "no_prior_plan"


async def test_renew_free_plan_not_renewable(db_session):
    identifier = "tg_700021"
    db_session.add(Tenant(phone=identifier, plan="free", subscription_status="active"))
    await db_session.commit()

    result = await bot_account_service.start_renew(db_session, telegram_user_id=700021)
    assert result.status == "no_prior_plan"


async def test_renew_reuses_current_paid_plan(db_session):
    identifier = "tg_700022"
    db_session.add(
        Tenant(
            phone=identifier,
            plan="team",
            subscription_status="expired",
            billing_period_end=_utcnow_naive() - timedelta(days=1),
        )
    )
    await db_session.commit()

    result = await bot_account_service.start_renew(db_session, telegram_user_id=700022)
    assert result.status == "ok"
    assert result.plan == "team"
    assert result.billing == "quarterly"


# ─── check_and_claim ─────────────────────────────────────────────────────


async def test_claim_no_tenant(db_session):
    result = await bot_account_service.check_and_claim(db_session, telegram_user_id=700030)
    assert result.status == "no_tenant"


async def test_claim_pending_payment(db_session):
    identifier = "tg_700031"
    db_session.add(Tenant(phone=identifier, plan="pro", subscription_status="pending", payment_ref="TM-X"))
    await db_session.commit()

    result = await bot_account_service.check_and_claim(db_session, telegram_user_id=700031)
    assert result.status == "pending"


async def test_claim_delivers_key_once(db_session):
    identifier = "tg_700032"
    tenant = Tenant(phone=identifier, plan="pro", subscription_status="active")
    db_session.add(tenant)
    await db_session.flush()

    raw_key = generate_user_api_key()
    api_key = APIKey(key=raw_key, name="USDT-pro-auto", is_active=True, tenant_id=tenant.id)
    db_session.add(api_key)
    await db_session.flush()

    db_session.add(
        PaymentRecord(
            tx_id="tx-700032",
            tenant_id=tenant.id,
            from_address="T-from",
            amount_usdt=10000,
            plan="pro",
            billing="monthly",
            status="completed",
            api_key_id=api_key.id,
            claimed=False,
        )
    )
    await db_session.commit()

    result = await bot_account_service.check_and_claim(db_session, telegram_user_id=700032)
    assert result.status == "claimed"
    assert result.api_key == raw_key

    # Claimed exactly once — a second call finds no unclaimed record left.
    result2 = await bot_account_service.check_and_claim(db_session, telegram_user_id=700032)
    assert result2.status == "no_payment"


# ─── list_purchase_history ───────────────────────────────────────────────


async def test_history_empty_for_unknown_user(db_session):
    records = await bot_account_service.list_purchase_history(db_session, telegram_user_id=700040)
    assert records == []


async def test_history_returns_records_newest_first(db_session):
    identifier = "tg_700041"
    tenant = Tenant(phone=identifier, plan="pro", subscription_status="active")
    db_session.add(tenant)
    await db_session.flush()

    older = PaymentRecord(
        tx_id="tx-older", tenant_id=tenant.id, from_address="a", amount_usdt=10000,
        plan="pro", status="completed", created_at=_utcnow_naive() - timedelta(days=30),
    )
    newer = PaymentRecord(
        tx_id="tx-newer", tenant_id=tenant.id, from_address="a", amount_usdt=10000,
        plan="pro", status="completed", created_at=_utcnow_naive(),
    )
    db_session.add_all([older, newer])
    await db_session.commit()

    records = await bot_account_service.list_purchase_history(db_session, telegram_user_id=700041)
    assert [r.tx_id for r in records] == ["tx-newer", "tx-older"]
