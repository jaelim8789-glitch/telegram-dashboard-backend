"""HTTP-level tests for the MCP Gateway REST endpoints.

Auth is bypassed via the shared `client` fixture (admin identity), so these
focus on routing, catalog shape, and approval gating through the real ASGI app.
"""

import pytest


async def test_mcp_gateway_catalog_endpoint(client):
    res = await client.get("/api/mcp-gateway/catalog")
    assert res.status_code == 200
    body = res.json()
    assert body["enabled"] is True
    ids = {s["server_id"] for s in body["servers"]}
    assert {"telegram", "grafana"} <= ids


async def test_mcp_gateway_invoke_unknown_server(client):
    res = await client.post(
        "/api/mcp-gateway/invoke",
        json={"server_id": "ghost", "tool_name": "x", "arguments": {}},
    )
    assert res.status_code in (502, 404)
    assert res.status_code != 200


async def test_mcp_gateway_approval_flow(client):
    # Enable telegram server for this request path.
    from app.services.mcp_gateway import gateway

    gateway.get("telegram").enabled = True

    # First call returns 202 Accepted with a pending_approval payload.
    res = await client.post(
        "/api/mcp-gateway/invoke",
        json={
            "server_id": "telegram",
            "tool_name": "send_message",
            "arguments": {"account_id": "x", "chat_id": "y", "text": "hi"},
        },
    )
    assert res.status_code == 202
    body = res.json()
    assert body.get("pending_approval") is True
    req_id = body["request_id"]

    # Approve via dedicated endpoint.
    res2 = await client.post("/api/mcp-gateway/approve", json={"request_id": req_id})
    assert res2.status_code == 200
    assert res2.json().get("ok") is True


async def test_mcp_gateway_approve_missing(client):
    res = await client.post("/api/mcp-gateway/approve", json={"request_id": "nope"})
    assert res.status_code == 404
