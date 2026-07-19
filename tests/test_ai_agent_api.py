"""Regression tests for POST /api/ai/messages/{message_id}/execute.

execute_tool() must actually run tools (not just log), reusing existing
delivery / broadcast logic:
- send     -> app.services.delivery.deliver_message
- schedule -> app.crud.broadcast.create_broadcast (real scheduled broadcast)

Covers: required-field validation, unsupported tool, and that the real
underlying service is invoked (mocked so no Telegram/network call happens).
"""

import pytest
from unittest.mock import AsyncMock, patch

from app.api.deps import Identity, get_current_identity
from app.main import app
from app.models.account import Account
from app.models.ai_agent import AiAgent, AiChat, AiMessage


async def _make_account_id(db_session, tenant_id, phone="+821000000001"):
    acct = Account(phone=phone, name="tool-test-account", tenant_id=tenant_id)
    db_session.add(acct)
    await db_session.commit()
    return acct.id


async def _make_agent(db_session, tenant_id):
    agent = AiAgent(owner_id=tenant_id, name="test agent", role="marketing")
    db_session.add(agent)
    await db_session.commit()
    await db_session.refresh(agent)
    return agent


async def _make_chat(db_session, agent_id, tenant_id):
    chat = AiChat(agent_id=agent_id, tenant_id=tenant_id, title="tool-chat")
    db_session.add(chat)
    await db_session.commit()
    await db_session.refresh(chat)
    return chat


async def _make_tool_message(db_session, chat_id, tool_name, tool_payload):
    msg = AiMessage(
        chat_id=chat_id,
        role="agent",
        content="response with tool button",
        tool_name=tool_name,
        tool_payload=tool_payload,
    )
    db_session.add(msg)
    await db_session.commit()
    await db_session.refresh(msg)
    return msg


async def _seed(db_session, client):
    # client fixture bypasses auth as admin; override the identity dependency
    # with a user identity scoped to the tenant we create resources under, so
    # the endpoint's tenant check (chat.tenant_id == identity.tenant_id) passes.
    tenant_id = "test-tenant-ai-agent"
    app.dependency_overrides[get_current_identity] = lambda: Identity(
        kind="user", tenant_id=tenant_id
    )
    account_id = await _make_account_id(db_session, tenant_id)
    agent = await _make_agent(db_session, tenant_id)
    chat = await _make_chat(db_session, agent.id, tenant_id)
    return tenant_id, account_id, agent, chat


async def test_execute_send_requires_account_id(db_session, client):
    tenant_id, account_id, agent, chat = await _seed(db_session, client)
    msg = await _make_tool_message(
        db_session, chat.id, "send",
        {"recipients": ["-1001"], "message": "hi"},  # no account_id
    )
    res = await client.post(f"/api/ai/messages/{msg.id}/execute")
    assert res.status_code == 422
    assert "account_id" in res.json()["detail"]


async def test_execute_send_requires_recipients(db_session, client):
    tenant_id, account_id, agent, chat = await _seed(db_session, client)
    msg = await _make_tool_message(
        db_session, chat.id, "send",
        {"account_id": account_id, "message": "hi"},  # no recipients
    )
    res = await client.post(f"/api/ai/messages/{msg.id}/execute")
    assert res.status_code == 422
    assert "recipients" in res.json()["detail"]


async def test_execute_send_invokes_deliver_message(db_session, client):
    tenant_id, account_id, agent, chat = await _seed(db_session, client)
    msg = await _make_tool_message(
        db_session, chat.id, "send",
        {"account_id": account_id, "recipients": ["-1001"], "message": "hello"},
    )

    from app.services.delivery import DeliveryStatus, DeliveryResult

    fake_results = [DeliveryResult(status=DeliveryStatus.SUCCESS, recipient="-1001", telegram_message_id=42)]
    with patch(
        "app.services.delivery.deliver_message", new=AsyncMock(return_value=fake_results)
    ) as mock_deliver:
        res = await client.post(f"/api/ai/messages/{msg.id}/execute")

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "executed"
    assert body["tool"] == "send"
    assert body["result"]["delivered"] == 1
    # Real delivery service was actually called (not just logged).
    mock_deliver.assert_awaited_once()
    req = mock_deliver.call_args.args[0]
    assert req.account_id == account_id
    assert req.recipients == ["-1001"]
    assert req.message == "hello"
    assert req.source == "ai_agent_tool"


async def test_execute_schedule_requires_scheduled_at(db_session, client):
    tenant_id, account_id, agent, chat = await _seed(db_session, client)
    msg = await _make_tool_message(
        db_session, chat.id, "schedule",
        {"account_id": account_id, "recipients": ["-1001"], "message": "hi"},  # no scheduled_at
    )
    res = await client.post(f"/api/ai/messages/{msg.id}/execute")
    assert res.status_code == 422
    assert "scheduled_at" in res.json()["detail"]


async def test_execute_schedule_requires_target(db_session, client):
    tenant_id, account_id, agent, chat = await _seed(db_session, client)
    msg = await _make_tool_message(
        db_session, chat.id, "schedule",
        {"account_id": account_id, "message": "hi", "scheduled_at": "2099-01-01T10:00:00"},
    )
    res = await client.post(f"/api/ai/messages/{msg.id}/execute")
    assert res.status_code == 422
    assert "recipients/group_ids" in res.json()["detail"]


async def test_execute_schedule_creates_real_broadcast(db_session, client):
    tenant_id, account_id, agent, chat = await _seed(db_session, client)
    msg = await _make_tool_message(
        db_session, chat.id, "schedule",
        {
            "account_id": account_id,
            "recipients": ["-1001"],
            "message": "scheduled broadcast message",
            "scheduled_at": "2099-01-01T10:00:00",
            "recurring_interval_minutes": 1440,
        },
    )

    created = {}

    def make_broadcast(sa):
        class _B:
            id = "bcast-123"
            status = "pending"
            scheduled_at = sa

        return _B()

    async def fake_create_broadcast(db, data, media_path, *, scheduled_at):
        created["data"] = data
        created["scheduled_at"] = scheduled_at
        return make_broadcast(scheduled_at)

    from app.crud import broadcast as broadcast_crud

    with patch.object(broadcast_crud, "create_broadcast", new=fake_create_broadcast):
        res = await client.post(f"/api/ai/messages/{msg.id}/execute")

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "executed"
    assert body["tool"] == "schedule"
    assert body["result"]["broadcast_id"] == "bcast-123"
    # Real broadcast CRUD was invoked with the payload.
    assert created["data"].account_id == account_id
    assert created["data"].message == "scheduled broadcast message"
    assert created["data"].recipients == ["-1001"]
    assert created["data"].recurring_interval_minutes == 1440
    assert created["scheduled_at"].year == 2099


async def test_execute_unsupported_tool_returns_400(db_session, client):
    tenant_id, account_id, agent, chat = await _seed(db_session, client)
    msg = await _make_tool_message(
        db_session, chat.id, "unknown_tool", {"foo": "bar"},
    )
    res = await client.post(f"/api/ai/messages/{msg.id}/execute")
    assert res.status_code == 400
    assert "Tool" in res.json()["detail"]


async def test_execute_message_without_tool_returns_400(db_session, client):
    tenant_id, account_id, agent, chat = await _seed(db_session, client)
    msg = AiMessage(chat_id=chat.id, role="agent", content="response without tool")
    db_session.add(msg)
    await db_session.commit()
    await db_session.refresh(msg)
    res = await client.post(f"/api/ai/messages/{msg.id}/execute")
    assert res.status_code == 400
