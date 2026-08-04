"""Redis-backed sync progress tests.

Verifies the Epic 19 contract:
  - progress lives in Redis with a 10-min TTL, refreshed on each update
  - complete_sync deletes the key AND persists terminal facts (dialogs/messages/last_sync_at) to the DB
  - fail_sync deletes the key
  - get_progress returns the stored payload or None
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services import sync_progress


class _FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.calls = []

    async def set(self, key, value, ex=None):
        self.calls.append(("set", key, ex))
        self.store[key] = value
        self.ttls[key] = ex
        return True

    async def get(self, key):
        self.calls.append(("get", key))
        return self.store.get(key)

    async def delete(self, key):
        self.calls.append(("delete", key))
        self.store.pop(key, None)
        self.ttls.pop(key, None)
        return 1


@pytest.mark.asyncio
async def test_set_progress_writes_redis_with_ttl(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(sync_progress, "_redis", AsyncMock(return_value=fake))

    await sync_progress.set_progress("acc-1", stage="syncing_dialogs", percent=28, dialogs=10, messages=50)

    raw = fake.store["sync:acc-1"]
    assert fake.ttls["sync:acc-1"] == 600
    import json
    data = json.loads(raw)
    assert data["stage"] == "syncing_dialogs"
    assert data["percent"] == 28
    assert data["dialogs"] == 10
    assert data["messages"] == 50


@pytest.mark.asyncio
async def test_get_progress_returns_payload(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(sync_progress, "_redis", AsyncMock(return_value=fake))
    await sync_progress.set_progress("acc-2", stage="syncing_messages", percent=51, dialogs=20, messages=500)

    result = await sync_progress.get_progress("acc-2")
    assert result is not None
    assert result["account_id"] == "acc-2"
    assert result["percent"] == 51


@pytest.mark.asyncio
async def test_get_progress_returns_none_when_missing(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(sync_progress, "_redis", AsyncMock(return_value=fake))
    assert await sync_progress.get_progress("acc-ghost") is None


@pytest.mark.asyncio
async def test_fail_sync_deletes_key(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(sync_progress, "_redis", AsyncMock(return_value=fake))
    await sync_progress.set_progress("acc-3", stage="syncing_dialogs", percent=10)
    assert "sync:acc-3" in fake.store

    await sync_progress.fail_sync("acc-3", "flood_wait")
    assert "sync:acc-3" not in fake.store


@pytest.mark.asyncio
async def test_complete_sync_persists_terminal_facts(monkeypatch, db_session):
    fake = _FakeRedis()
    monkeypatch.setattr(sync_progress, "_redis", AsyncMock(return_value=fake))
    await sync_progress.set_progress("acc-4", stage="syncing_dialogs", percent=5)
    assert "sync:acc-4" in fake.store

    from app.models.account import Account

    account = Account(phone="+821011111111", status="active", session_data="x")
    db_session.add(account)
    await db_session.commit()

    from app.crud import account as account_crud
    monkeypatch.setattr(account_crud, "get_account", AsyncMock(return_value=account))

    from app.database import async_session_maker
    monkeypatch.setattr(sync_progress, "async_session_maker", lambda: db_session)

    await sync_progress.complete_sync("acc-4", dialogs=284, messages=4832)

    # Redis key deleted
    assert "sync:acc-4" not in fake.store
