import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routes.ws import ws_router
from app.realtime.dispatcher import dispatcher


@pytest.fixture
def ws_app(monkeypatch):
    # WS connections now require a valid auth token (hard enforcement) and
    # tenant ownership of the subscribed account. These tests exercise the
    # broadcast plumbing, not auth/authorization — so bypass both gates.
    from app.api.deps import Identity

    async def _bypass_gate(websocket, token):
        return Identity(kind="admin")

    async def _bypass_access(websocket, identity, account_id):
        return True

    monkeypatch.setattr("app.routes.ws._ws_auth_gate", _bypass_gate)
    monkeypatch.setattr("app.routes.ws._verify_account_access", _bypass_access)

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
