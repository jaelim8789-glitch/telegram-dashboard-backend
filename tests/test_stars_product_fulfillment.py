"""Fulfillment of Telegram Stars purchases from the STAR_PRODUCTS catalog
(plan subscriptions, AI credit boosts) via app.bot.service._handle_successful_payment.

Regression context: this handler used to call AdminPlatform, a separate
sqlite-backed store disconnected from the real Postgres Tenant the rest of
the app reads plan/limits/AI-credit-balance from -- a paying Stars customer's
Tenant.plan and Tenant.ai_credits_remaining never actually changed. AI Boost
purchases additionally called record_usage(api_calls=0), always zero, so they
delivered nothing at all. These tests verify the real Tenant row is what gets
updated, and that a redelivered successful_payment update is a no-op.
"""

import itertools

import pytest

from app.bot.service import _handle_successful_payment
from app.models.tenant import Tenant
from app.services.usage_tracker import apply_plan_limits

_phone_seq = itertools.count(1)


async def _make_tenant(db, *, plan="free", **overrides):
    tenant = Tenant(phone=overrides.pop("phone", f"+8219{next(_phone_seq):08d}"))
    db.add(tenant)
    await db.flush()
    await apply_plan_limits(db, tenant, plan)
    for key, value in overrides.items():
        setattr(tenant, key, value)
    await db.commit()
    await db.refresh(tenant)
    return tenant


class db_session_cm:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


def _successful_payment_message(*, payload: str, charge_id: str, stars: int) -> dict:
    return {
        "successful_payment": {
            "invoice_payload": payload,
            "telegram_payment_charge_id": charge_id,
            "total_amount": stars,
        }
    }


@pytest.mark.asyncio
async def test_plan_purchase_upgrades_real_tenant(db_session, monkeypatch):
    import app.bot.service as service_module

    monkeypatch.setattr(service_module, "async_session_maker", lambda: db_session_cm(db_session))
    tenant = await _make_tenant(db_session, plan="free")

    import json
    payload = json.dumps({"pid": "pro_monthly", "tid": tenant.id})
    await _handle_successful_payment(None, _successful_payment_message(payload=payload, charge_id="charge-plan-1", stars=450))

    await db_session.refresh(tenant)
    assert tenant.plan == "pro"
    assert tenant.subscription_status == "active"
    assert tenant.billing_period_end is not None


@pytest.mark.asyncio
async def test_ai_boost_purchase_credits_real_ai_balance(db_session, monkeypatch):
    import app.bot.service as service_module

    monkeypatch.setattr(service_module, "async_session_maker", lambda: db_session_cm(db_session))
    tenant = await _make_tenant(db_session, plan="free", ai_credits_remaining=10)

    import json
    payload = json.dumps({"pid": "ai_boost_1000", "tid": tenant.id})
    await _handle_successful_payment(None, _successful_payment_message(payload=payload, charge_id="charge-ai-1", stars=300))

    await db_session.refresh(tenant)
    assert tenant.ai_credits_remaining == 1010


@pytest.mark.asyncio
async def test_redelivered_charge_id_does_not_double_credit(db_session, monkeypatch):
    import app.bot.service as service_module

    monkeypatch.setattr(service_module, "async_session_maker", lambda: db_session_cm(db_session))
    tenant = await _make_tenant(db_session, plan="free", ai_credits_remaining=0)

    import json
    payload = json.dumps({"pid": "ai_boost_1000", "tid": tenant.id})
    msg = _successful_payment_message(payload=payload, charge_id="charge-dup-1", stars=300)
    await _handle_successful_payment(None, msg)
    await _handle_successful_payment(None, msg)

    await db_session.refresh(tenant)
    assert tenant.ai_credits_remaining == 1000


@pytest.mark.asyncio
async def test_resolves_tenant_via_chat_id_when_no_tenant_id_in_payload(db_session, monkeypatch):
    """The bot's own /buy command flow doesn't know the tenant_id -- only the
    chat_id -- and must still resolve via the tg_<chat_id> phone convention."""
    import app.bot.service as service_module

    monkeypatch.setattr(service_module, "async_session_maker", lambda: db_session_cm(db_session))
    tenant = await _make_tenant(db_session, plan="free", phone="tg_555444333", ai_credits_remaining=0)

    import json
    payload = json.dumps({"pid": "ai_boost_1000", "cid": 555444333})
    await _handle_successful_payment(None, _successful_payment_message(payload=payload, charge_id="charge-cid-1", stars=300))

    await db_session.refresh(tenant)
    assert tenant.ai_credits_remaining == 1000
