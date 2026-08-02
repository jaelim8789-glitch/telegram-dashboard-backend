import asyncio
import sys
import types

import pytest

from app.services.backoff import BackoffStrategy
from app.services.connection_service import ConnectionService
from app.services.health_service import HealthService
from app.services.lock_manager import LockManager
from app.services.state_machine import SessionState, transition


class AsyncBus:
    async def publish(self, *args, **kwargs):
        return None


class FakePool:
    def __init__(self):
        self.clients = {}
        self.disconnected = []
        self.get_client_calls = {}

    async def get_client(self, account_id, session_string="", require_authorized=True):
        self.get_client_calls[account_id] = self.get_client_calls.get(account_id, 0) + 1
        if account_id not in self.clients:
            self.clients[account_id] = object()
        await asyncio.sleep(0.01)
        return self.clients[account_id]

    async def disconnect(self, account_id):
        self.disconnected.append(account_id)
        self.clients.pop(account_id, None)


def test_reconnect_is_allowed_from_connected_state():
    result = transition(SessionState.CONNECTED, "reconnect")
    assert result.valid is True
    assert result.current is SessionState.RECONNECTING


def test_disconnect_is_allowed_from_connecting_state():
    result = transition(SessionState.CONNECTING, "disconnect")
    assert result.valid is True
    assert result.current is SessionState.DISCONNECTED


@pytest.mark.asyncio
async def test_connect_from_expired_state_retries_with_reauth_transition(monkeypatch):
    fake_pool = FakePool()
    fake_module = types.SimpleNamespace(pool=fake_pool)
    monkeypatch.setitem(sys.modules, "app.services.telethon_pool", fake_module)

    service = ConnectionService(event_bus=AsyncBus(), lock_manager=LockManager(), backoff=BackoffStrategy())
    service._states["acc-1"] = SessionState.EXPIRED

    state = await service.connect("acc-1")

    assert state is SessionState.CONNECTED
    assert service.get_state("acc-1") is SessionState.CONNECTED


@pytest.mark.asyncio
async def test_multi_account_connects_remain_independent(monkeypatch):
    fake_pool = FakePool()
    fake_module = types.SimpleNamespace(pool=fake_pool)
    monkeypatch.setitem(sys.modules, "app.services.telethon_pool", fake_module)

    service = ConnectionService(event_bus=AsyncBus(), lock_manager=LockManager(), backoff=BackoffStrategy())
    account_ids = [f"acc-{i}" for i in range(20)]

    states = await asyncio.gather(*(service.connect(account_id) for account_id in account_ids))

    assert states == [SessionState.CONNECTED] * len(account_ids)
    assert all(fake_pool.get_client_calls[account_id] == 1 for account_id in account_ids)


@pytest.mark.asyncio
async def test_duplicate_connect_requests_do_not_create_multiple_clients(monkeypatch):
    fake_pool = FakePool()
    fake_module = types.SimpleNamespace(pool=fake_pool)
    monkeypatch.setitem(sys.modules, "app.services.telethon_pool", fake_module)

    service = ConnectionService(event_bus=AsyncBus(), lock_manager=LockManager(), backoff=BackoffStrategy())

    states = await asyncio.gather(service.connect("acc-1"), service.connect("acc-1"))

    assert states == [SessionState.CONNECTED, SessionState.CONNECTED]
    assert fake_pool.get_client_calls["acc-1"] == 1


@pytest.mark.asyncio
async def test_duplicate_reconnect_requests_do_not_create_multiple_clients(monkeypatch):
    fake_pool = FakePool()
    fake_module = types.SimpleNamespace(pool=fake_pool)
    monkeypatch.setitem(sys.modules, "app.services.telethon_pool", fake_module)

    service = ConnectionService(event_bus=AsyncBus(), lock_manager=LockManager(), backoff=BackoffStrategy())
    service._states["acc-1"] = SessionState.CONNECTED

    states = await asyncio.gather(service.reconnect("acc-1"), service.reconnect("acc-1"))

    assert states == [SessionState.CONNECTED, SessionState.CONNECTED]
    assert fake_pool.get_client_calls["acc-1"] == 1


@pytest.mark.asyncio
async def test_disconnect_cleans_up_runtime_client(monkeypatch):
    fake_pool = FakePool()
    fake_module = types.SimpleNamespace(pool=fake_pool)
    monkeypatch.setitem(sys.modules, "app.services.telethon_pool", fake_module)

    service = ConnectionService(event_bus=AsyncBus(), lock_manager=LockManager(), backoff=BackoffStrategy())
    service._states["acc-1"] = SessionState.CONNECTED

    await service.disconnect("acc-1")

    assert "acc-1" not in fake_pool.clients
    assert "acc-1" in fake_pool.disconnected


def test_expired_state_health_is_reported_as_expired():
    health = HealthService().derive_state_health(SessionState.EXPIRED)

    assert health["status"] == "expired"
