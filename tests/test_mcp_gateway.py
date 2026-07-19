"""Tests for the MCP Gateway, Telegram MCP (PoC), and Grafana MCP servers."""

import pytest

from app.mcp.base import MCPTool, MCPToolResult
from app.mcp.grafana_mcp import GrafanaMCPServer
from app.mcp.telegram_mcp import TelegramMCPServer
from app.services.mcp_gateway import MCPGateway


# ─── Catalog / registry ──────────────────────────────────────────────────────


def test_gateway_registers_default_servers():
    gw = MCPGateway()
    assert gw.get("telegram") is not None
    assert gw.get("grafana") is not None
    catalog = gw.catalog()
    ids = {s["server_id"] for s in catalog.servers}
    assert {"telegram", "grafana"} <= ids


def test_gateway_catalog_shape():
    gw = MCPGateway()
    catalog = gw.catalog()
    assert catalog.enabled is True
    for server in catalog.servers:
        assert "server_id" in server and "title" in server and "tools" in server
        assert isinstance(server["tools"], list)


# ─── Telegram MCP PoC ────────────────────────────────────────────────────────


def test_telegram_mcp_lists_expected_tools():
    server = TelegramMCPServer()
    names = {t.name for t in server.list_tools()}
    assert {"list_accounts", "account_health", "send_message"} <= names


def test_telegram_mcp_send_requires_approval():
    server = TelegramMCPServer()
    send = next(t for t in server.list_tools() if t.name == "send_message")
    assert send.requires_approval is True


@pytest.mark.asyncio
async def test_telegram_mcp_unknown_tool():
    server = TelegramMCPServer()
    result = await server.invoke("does_not_exist", {})
    assert result.ok is False
    assert "unknown tool" in result.error


# ─── Grafana MCP ─────────────────────────────────────────────────────────────


def test_grafana_mcp_lists_expected_tools():
    server = GrafanaMCPServer()
    names = {t.name for t in server.list_tools()}
    assert {"query_prometheus", "query_range", "query_logs", "list_dashboards"} <= names


@pytest.mark.asyncio
async def test_grafana_mcp_not_configured_reports_clearly():
    server = GrafanaMCPServer()
    result = await server.invoke("query_prometheus", {"query": "up"})
    # Either properly reports not-configured, or (if token set) hits network
    # and returns an error — both are non-crashing.
    assert result.ok is False
    assert "not configured" in (result.error or "") or "failed" in (result.error or "")


# ─── Gateway approval gating ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gateway_gates_approval_tools():
    gw = MCPGateway()
    # Force telegram server enabled for this test.
    gw.get("telegram").enabled = True

    result = await gw.invoke("telegram", "send_message", {"account_id": "x", "chat_id": "y", "text": "hi"})
    assert result.get("pending_approval") is True
    req_id = result["request_id"]
    # Approving executes (PoC defers external send, returns routed=True).
    # The real approve path (API) re-uses the pending entry's arguments.
    pending = gw.get_pending(req_id)
    assert pending is not None
    approved = await gw.invoke(
        pending.server_id, pending.tool_name, pending.arguments, approved=True
    )
    assert approved.get("ok") is True


@pytest.mark.asyncio
async def test_gateway_unknown_server_errors():
    gw = MCPGateway()
    result = await gw.invoke("nope", "x", {})
    assert result.get("ok") is False
    assert "unknown server" in result["error"]
