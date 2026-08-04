"""_resolve_telegram_chat must only ever resolve the CALLER's own linked
Telegram chat -- never any other user's.

Regression context: this used to query bot_sessions for whichever chat was
most recently updated GLOBALLY (no relation to the caller), so a user
creating a Stars invoice without an explicit telegram_chat_id could have it
sent to an unrelated user's Telegram chat.
"""

import itertools

import pytest

from app.api.deps import Identity
from app.models.tenant import Tenant
from app.models.user import User
from app.routers.stars_payments import _resolve_telegram_chat
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


@pytest.mark.asyncio
async def test_resolves_via_user_telegram_id_when_linked(db_session, monkeypatch):
    import app.routers.stars_payments as module
    import app.database as database_module
    monkeypatch.setattr(database_module, "async_session_maker", lambda: db_session_cm(db_session))

    tenant = await _make_tenant(db_session)
    user = User(phone="+821011112222", telegram_id=987654321)
    identity = Identity(kind="user", user=user, tenant_id=tenant.id)

    chat_id = await _resolve_telegram_chat(identity)
    assert chat_id == 987654321


@pytest.mark.asyncio
async def test_resolves_via_tg_phone_convention_when_no_linked_user_telegram_id(db_session, monkeypatch):
    import app.routers.stars_payments as module
    import app.database as database_module
    monkeypatch.setattr(database_module, "async_session_maker", lambda: db_session_cm(db_session))

    tenant = await _make_tenant(db_session, phone="tg_112233445")
    identity = Identity(kind="user", user=None, tenant_id=tenant.id)

    chat_id = await _resolve_telegram_chat(identity)
    assert chat_id == 112233445


@pytest.mark.asyncio
async def test_returns_none_when_caller_has_no_telegram_link(db_session, monkeypatch):
    """Must NOT fall back to some other user's chat -- a real phone-signup
    tenant with no Telegram link at all resolves to nothing."""
    import app.routers.stars_payments as module
    import app.database as database_module
    monkeypatch.setattr(database_module, "async_session_maker", lambda: db_session_cm(db_session))

    # A different tenant exists (simulating "someone else recently used the
    # bot") to prove it's never picked up as a fallback.
    await _make_tenant(db_session, phone="tg_999888777")

    tenant = await _make_tenant(db_session, phone="+821099998888")
    identity = Identity(kind="user", user=None, tenant_id=tenant.id)

    chat_id = await _resolve_telegram_chat(identity)
    assert chat_id is None
