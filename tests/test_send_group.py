"""Tests for the Send-to-Group broadcast workflow.

Covers:
1. Broadcast creation with group_ids instead of recipients
2. Send-group API endpoint (POST /api/broadcast/send-group)
3. Group member resolution in broadcast_processor
4. Broadcast estimate endpoint
5. Batch retry endpoint
6. Scheduler status endpoint
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from app.crud import broadcast as broadcast_crud
from app.schemas.broadcast import BroadcastCreate


async def _create_account(client, phone="+821088880000"):
    res = await client.post("/api/accounts", json={"phone": phone, "name": "발송 테스트 계정"})
    assert res.status_code == 201
    return res.json()["id"]


# ═══════════════════════════════════════════════════════════════════════
# 1. Broadcast creation with group_ids
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_broadcast_creation_with_group_ids(db_session):
    """Creating a broadcast with group_ids stores them correctly."""
    from app.crud import account as account_crud
    from app.schemas.account import AccountCreate

    account = await account_crud.create_account(db_session, AccountCreate(phone="+821088880001"))

    payload = BroadcastCreate(
        account_id=account.id,
        message="테스트 그룹 발송",
        recipients=[],
        group_ids=["-100111", "-100222"],
    )

    broadcast = await broadcast_crud.create_broadcast(db_session, payload, media_path=None, scheduled_at=None)
    assert broadcast.group_ids == ["-100111", "-100222"]
    assert broadcast.groups_resolved is False
    assert broadcast.recipients == []


@pytest.mark.asyncio
async def test_broadcast_creation_with_recipients_and_group_ids(db_session):
    """Creating a broadcast with both recipients and group_ids works."""
    from app.crud import account as account_crud
    from app.schemas.account import AccountCreate

    account = await account_crud.create_account(db_session, AccountCreate(phone="+821088880002"))

    payload = BroadcastCreate(
        account_id=account.id,
        message="하이브리드 발송",
        recipients=["-100333"],
        group_ids=["-100111"],
    )

    broadcast = await broadcast_crud.create_broadcast(db_session, payload, media_path=None, scheduled_at=None)
    assert broadcast.group_ids == ["-100111"]
    assert broadcast.recipients == ["-100333"]


@pytest.mark.asyncio
async def test_broadcast_creation_without_recipients_or_group_ids_raises_error():
    """Creating a broadcast without recipients or group_ids must be rejected."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="recipients or group_ids is required"):
        BroadcastCreate(
            account_id="test-account",
            message="테스트",
            recipients=[],
        )


# ═══════════════════════════════════════════════════════════════════════
# 2. Send-group API endpoint
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_send_group_endpoint_creates_broadcast(client):
    """POST /api/broadcast/send-group should create a broadcast with group_ids."""
    account_id = await _create_account(client)

    payload = {
        "account_id": account_id,
        "message": "그룹 발송 테스트",
        "group_ids": ["-100111", "-100222"],
    }
    res = await client.post("/api/broadcast/send-group", json=payload)
    assert res.status_code == 202, res.text
    body = res.json()
    assert body["account_id"] == account_id
    assert body["group_ids"] == ["-100111", "-100222"]
    assert body["groups_resolved"] is False
    assert body["status"] == "pending"


@pytest.mark.asyncio
async def test_send_group_endpoint_with_schedule(client):
    """Send-group with a future scheduled_at should not trigger immediate dispatch."""
    account_id = await _create_account(client)
    future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()

    payload = {
        "account_id": account_id,
        "message": "예약 그룹 발송",
        "group_ids": ["-100333"],
        "scheduled_at": future,
    }
    res = await client.post("/api/broadcast/send-group", json=payload)
    assert res.status_code == 202, res.text
    body = res.json()
    assert body["scheduled_at"] is not None


@pytest.mark.asyncio
async def test_send_group_endpoint_unknown_account(client):
    """Send-group with unknown account returns 404."""
    payload = {
        "account_id": "non-existent",
        "message": "테스트",
        "group_ids": ["-100111"],
    }
    res = await client.post("/api/broadcast/send-group", json=payload)
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_send_group_endpoint_validates_group_ids(client):
    """Send-group requires at least one group_id."""
    account_id = await _create_account(client)

    payload = {
        "account_id": account_id,
        "message": "테스트",
        "group_ids": [],
    }
    res = await client.post("/api/broadcast/send-group", json=payload)
    assert res.status_code == 422


# ═══════════════════════════════════════════════════════════════════════
# 3. Group member resolution
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_resolve_group_ids_to_recipients(monkeypatch):
    """resolve_group_ids_to_recipients should return member IDs from groups."""
    from app.services.broadcast_processor import resolve_group_ids_to_recipients

    class FakeUser:
        def __init__(self, id):
            self.id = id

    async def mock_list_group_members(account, group_id):
        if group_id == "-100111":
            return [FakeUser(111), FakeUser(222), FakeUser(333)]
        elif group_id == "-100222":
            return [FakeUser(444), FakeUser(555)]
        return []

    monkeypatch.setattr("app.services.broadcast_processor.list_group_members", mock_list_group_members)

    members = await resolve_group_ids_to_recipients("fake-account", ["-100111", "-100222"])
    assert "111" in members
    assert "222" in members
    assert "333" in members
    assert "444" in members
    assert "555" in members
    assert len(members) == 5


@pytest.mark.asyncio
async def test_resolve_group_ids_empty_groups_returns_empty(monkeypatch):
    """When no groups resolve, returns empty list."""
    from app.services.broadcast_processor import resolve_group_ids_to_recipients

    async def mock_list_group_members(account, group_id):
        return []

    monkeypatch.setattr("app.services.broadcast_processor.list_group_members", mock_list_group_members)

    members = await resolve_group_ids_to_recipients("fake-account", ["-100333"])
    assert members == []


@pytest.mark.asyncio
async def test_resolve_group_ids_with_failure_returns_partial(monkeypatch):
    """When one group fails, the others should still resolve."""
    from app.services.broadcast_processor import resolve_group_ids_to_recipients

    class FakeUser:
        def __init__(self, id):
            self.id = id

    async def mock_list_group_members(account, group_id):
        if group_id == "-100111":
            return [FakeUser(111)]
        elif group_id == "-100222":
            raise Exception("Access denied")
        return []

    monkeypatch.setattr("app.services.broadcast_processor.list_group_members", mock_list_group_members)

    members = await resolve_group_ids_to_recipients("fake-account", ["-100111", "-100222"])
    assert "111" in members
    assert len(members) == 1


# ═══════════════════════════════════════════════════════════════════════
# 4. Broadcast estimate endpoint
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_broadcast_estimate_normal_mode(client):
    account_id = await _create_account(client)
    payload = {"account_id": account_id, "recipient_count": 10, "delivery_mode": "normal"}
    res = await client.post("/api/broadcast/estimate", json=payload)
    assert res.status_code == 200
    body = res.json()
    assert body["estimated_seconds"] >= 1
    assert body["readable"] is not None


@pytest.mark.asyncio
async def test_broadcast_estimate_bulk_mode(client):
    account_id = await _create_account(client)
    payload = {"account_id": account_id, "recipient_count": 100, "delivery_mode": "bulk"}
    res = await client.post("/api/broadcast/estimate", json=payload)
    assert res.status_code == 200
    body = res.json()
    assert body["estimated_seconds"] > 0


@pytest.mark.asyncio
async def test_broadcast_estimate_unknown_account(client):
    payload = {"account_id": "no-such", "recipient_count": 10, "delivery_mode": "normal"}
    res = await client.post("/api/broadcast/estimate", json=payload)
    assert res.status_code == 404


# ═══════════════════════════════════════════════════════════════════════
# 5. Batch retry endpoint
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_batch_retry_skips_non_failed(client, db_session):
    """Batch retry of broadcasts that aren't failed should skip them."""
    from app.crud import account as account_crud
    from app.schemas.account import AccountCreate

    account = await account_crud.create_account(db_session, AccountCreate(phone="+821088880010"))
    payload_broadcast = BroadcastCreate(
        account_id=account.id,
        message="테스트",
        recipients=["-100111"],
    )
    broadcast = await broadcast_crud.create_broadcast(db_session, payload_broadcast, media_path=None, scheduled_at=None)
    broadcast.status = "sent"
    await db_session.commit()

    payload = {"broadcast_ids": [broadcast.id]}
    # Override identity to admin
    from app.api.deps import get_current_identity, Identity
    from app.main import app
    import copy

    res = await client.post("/api/broadcast/batch-retry", json=payload)
    assert res.status_code == 200
    body = res.json()
    assert body["results"][0]["status"] == "skipped"


@pytest.mark.asyncio
async def test_batch_retry_empty_list_returns_422(client):
    """Empty broadcast_ids list should fail validation."""
    payload = {"broadcast_ids": []}
    res = await client.post("/api/broadcast/batch-retry", json=payload)
    assert res.status_code == 422


# ═══════════════════════════════════════════════════════════════════════
# 6. Scheduler status endpoint
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_scheduler_status_endpoint(client):
    """GET /api/scheduler/status returns scheduler info."""
    res = await client.get("/api/scheduler/status")
    assert res.status_code == 200
    body = res.json()
    assert "tick_interval_seconds" in body
    assert "due_broadcasts_count" in body
    assert "running_broadcasts_count" in body
    assert "scheduler_running" in body


@pytest.mark.asyncio
async def test_scheduler_upcoming_returns_list(client):
    """GET /api/scheduler/upcoming returns a list."""
    res = await client.get("/api/scheduler/upcoming")
    assert res.status_code == 200
    assert isinstance(res.json(), list)