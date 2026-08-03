import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routes.ws import ws_router
from app.realtime.dispatcher import dispatcher


@pytest.fixture
def ws_app(monkeypatch):
    # WS connections now require a valid auth token (hard enforcement). These
    # tests exercise the broadcast plumbing, not auth — so bypass the gate.
    async def _bypass_gate(websocket, token):
        return True
    monkeypatch.setattr("app.routes.ws._ws_auth_gate", _bypass_gate)

    app = FastAPI()
    app.include_router(ws_router)
    return app


def test_dialog_update_broadcasts_to_ws_clients(ws_app):
    client = TestClient(ws_app)
    with client.websocket_connect("/ws/chat?account_id=account-1") as websocket:
        # simulate a dialog_update from the server side
        asyncio.run(__import__("app.routes.ws", fromlist=["broadcast_dialog_update"]).broadcast_dialog_update("account-1"))

        message = websocket.receive_json()
        assert message["type"] == "dialog_update"
