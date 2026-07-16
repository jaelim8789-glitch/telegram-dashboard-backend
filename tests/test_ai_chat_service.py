"""Tests for app.services.ai_chat_service — the bot "🤖 AI Chat" DeepSeek flow.

Mirrors the fixture/helper style of tests/test_billing_entitlements.py:
_make_tenant + db_session_cm to let a service that opens its own
async_session_maker() session (usage_tracker.record_usage) reuse this test's
in-transaction session/engine.
"""

from sqlalchemy import select

import pytest

from app.core.telegram_identity import tg_identifier
from app.models.tenant import AiChatMessage, Tenant, UsageRecord
from app.services.usage_tracker import apply_plan_limits

import app.services.ai_chat_service as ai_chat_module
import app.services.usage_tracker as usage_tracker_module


class db_session_cm:
    """Wrap an already-open test db_session as an async-context-manager, matching
    async_session_maker()'s call signature, so record_usage (which opens its own
    session) reuses the same in-test transaction/engine."""

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


async def _make_tenant(db, telegram_user_id: int, *, plan="free", **overrides):
    overrides.setdefault("subscription_status", "active")
    tenant = Tenant(phone=tg_identifier(telegram_user_id))
    db.add(tenant)
    await db.flush()
    await apply_plan_limits(db, tenant, plan)
    for key, value in overrides.items():
        setattr(tenant, key, value)
    await db.commit()
    await db.refresh(tenant)
    return tenant


def _patch_common(monkeypatch, db_session, *, deepseek_reply="테스트 응답입니다.", deepseek_key="test-key"):
    monkeypatch.setattr(usage_tracker_module, "async_session_maker", lambda: db_session_cm(db_session))
    monkeypatch.setattr(ai_chat_module.settings, "deepseek_api_key", deepseek_key)

    calls: list[list[dict]] = []

    async def _fake_deepseek(messages):
        calls.append(messages)
        return deepseek_reply

    monkeypatch.setattr(ai_chat_module, "_call_deepseek", _fake_deepseek)
    return calls


async def _message_count(db, tenant_id: str) -> int:
    result = await db.execute(select(AiChatMessage).where(AiChatMessage.tenant_id == tenant_id))
    return len(result.scalars().all())


async def _seed_usage(db, tenant_id: str, count: int) -> None:
    db.add(UsageRecord(tenant_id=tenant_id, action="ai_chat", count=count))
    await db.commit()


@pytest.mark.asyncio
async def test_send_message_success_within_quota(db_session, monkeypatch):
    telegram_user_id = 111
    tenant = await _make_tenant(db_session, telegram_user_id, plan="free")
    calls = _patch_common(monkeypatch, db_session)

    result = await ai_chat_module.send_message(db_session, telegram_user_id, "안녕")

    assert result.status == "ok"
    assert result.reply == "테스트 응답입니다."
    assert len(calls) == 1
    assert await _message_count(db_session, tenant.id) == 2


@pytest.mark.asyncio
async def test_quota_exceeded_without_credit_blocks_call(db_session, monkeypatch):
    telegram_user_id = 222
    tenant = await _make_tenant(db_session, telegram_user_id, plan="free")  # monthly_ai_chat_limit=20
    await _seed_usage(db_session, tenant.id, count=20)
    calls = _patch_common(monkeypatch, db_session)

    result = await ai_chat_module.send_message(db_session, telegram_user_id, "안녕")

    assert result.status == "quota_exceeded"
    assert calls == []
    assert await _message_count(db_session, tenant.id) == 0


@pytest.mark.asyncio
async def test_quota_exceeded_spends_credit_and_succeeds(db_session, monkeypatch):
    telegram_user_id = 333
    tenant = await _make_tenant(db_session, telegram_user_id, plan="free", ai_chat_credit_balance=5)
    await _seed_usage(db_session, tenant.id, count=20)
    calls = _patch_common(monkeypatch, db_session)

    result = await ai_chat_module.send_message(db_session, telegram_user_id, "안녕")

    assert result.status == "ok"
    assert len(calls) == 1
    await db_session.refresh(tenant)
    assert tenant.ai_chat_credit_balance == 4


@pytest.mark.asyncio
async def test_not_linked_tenant_returns_not_linked(db_session, monkeypatch):
    _patch_common(monkeypatch, db_session)

    result = await ai_chat_module.send_message(db_session, 999999, "안녕")

    assert result.status == "not_linked"


@pytest.mark.asyncio
async def test_deepseek_failure_returns_server_error_without_charging_quota(db_session, monkeypatch):
    telegram_user_id = 444
    tenant = await _make_tenant(db_session, telegram_user_id, plan="free")
    monkeypatch.setattr(usage_tracker_module, "async_session_maker", lambda: db_session_cm(db_session))
    monkeypatch.setattr(ai_chat_module.settings, "deepseek_api_key", "test-key")

    async def _failing_deepseek(messages):
        return None

    monkeypatch.setattr(ai_chat_module, "_call_deepseek", _failing_deepseek)

    result = await ai_chat_module.send_message(db_session, telegram_user_id, "안녕")

    assert result.status == "server_error"
    assert await _message_count(db_session, tenant.id) == 0
    from app.services.usage_tracker import get_monthly_usage

    assert await get_monthly_usage(db_session, tenant.id, action="ai_chat") == 0


@pytest.mark.asyncio
async def test_input_too_long_rejected_before_any_call(db_session, monkeypatch):
    calls = _patch_common(monkeypatch, db_session)

    result = await ai_chat_module.send_message(db_session, 555, "가" * 2001)

    assert result.status == "too_long"
    assert calls == []
