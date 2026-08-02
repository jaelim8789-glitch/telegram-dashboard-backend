import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routes.ws import ws_router


@pytest.fixture
def ws_app():
    app = FastAPI()
    app.include_router(ws_router)
    return app


def test_ws_sessions_broadcasts_session_events(ws_app):
    client = TestClient(ws_app)
    with client.websocket_connect("/ws/sessions") as websocket:
        initial = websocket.receive_json()
        assert initial["type"] == "session_states"

        payload = {
            "account_id": "acc-42",
            "event": "session.connected",
            "state": "connected",
            "reason": "register",
        }

        import asyncio
        asyncio.run(__import__("app.routes.ws", fromlist=["broadcast_session_event"]).broadcast_session_event(payload))

        message = websocket.receive_json()
        assert message["type"] == "session_event"
        assert message["account_id"] == "acc-42"
        assert message["state"] == "connected"
        assert message["event"] == "session.connected"
