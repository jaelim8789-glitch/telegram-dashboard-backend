import sys
import types

import pytest

from app.services.backoff import BackoffStrategy
from app.services.connection_service import ConnectionService
from app.services.lock_manager import LockManager
from app.services.state_machine import SessionState, transition


class AsyncBus:
    async def publish(self, *args, **kwargs):
        return None


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
    class FakePool:
        async def get_client(self, account_id, session_string="", require_authorized=True):
            return object()

    fake_module = types.SimpleNamespace(pool=FakePool())
    monkeypatch.setitem(sys.modules, "app.services.telethon_pool", fake_module)

    service = ConnectionService(event_bus=AsyncBus(), lock_manager=LockManager(), backoff=BackoffStrategy())
    service._states["acc-1"] = SessionState.EXPIRED

    state = await service.connect("acc-1")

    assert state is SessionState.CONNECTED
    assert service.get_state("acc-1") is SessionState.CONNECTED
