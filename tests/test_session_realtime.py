from types import SimpleNamespace

import pytest

from app.services.session_manager import SessionManager


@pytest.mark.asyncio
async def test_session_manager_forwards_session_events_to_websocket_broadcast(monkeypatch):
    manager = SessionManager()
    manager._event_logger = None
    manager._metrics = SimpleNamespace(record_event=lambda: None)

    captured = {}

    async def fake_broadcast(payload):
        captured["payload"] = payload

    monkeypatch.setattr("app.routes.ws.broadcast_session_event", fake_broadcast)

    await manager._on_any_session_event({
        "account_id": "acc-123",
        "event": "session.connected",
        "state": "connected",
        "reason": "register",
    })

    assert captured["payload"]["account_id"] == "acc-123"
    assert captured["payload"]["event"] == "session.connected"
    assert captured["payload"]["state"] == "connected"
    assert captured["payload"]["reason"] == "register"
