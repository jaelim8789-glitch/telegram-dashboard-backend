"""
MCP Client — connects to external MCP servers for tool discovery and execution.

This allows the AI platform to call tools from any MCP-compatible server,
extending the AI's capabilities with external tool ecosystems.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.ai.config import get_ai_config
from app.core.logging import get_logger

logger = get_logger(__name__)


class McpClient:
    """Client for interacting with MCP (Model Context Protocol) servers.

    Supports tool discovery and tool execution via the MCP protocol.
    """

    def __init__(self) -> None:
        self._config = get_ai_config()
        self._tools_cache: list[dict[str, Any]] | None = None
        self._server_url: str = ""

    async def discover_tools(self, server_url: str | None = None) -> list[dict[str, Any]]:
        """Discover available tools from an MCP server.

        Returns a list of tool definitions compatible with the tool registry.
        """
        url = server_url or self._config.mcp_server_url
        if not url:
            logger.warning("mcp_no_server_url_configured")
            return []

        try:
            async with httpx.AsyncClient(timeout=self._config.mcp_request_timeout) as client:
                response = await client.get(
                    f"{url.rstrip('/')}/tools",
                    headers=self._build_headers(),
                )
                response.raise_for_status()
                data = response.json()
                tools = data.get("tools", data if isinstance(data, list) else [])
                self._tools_cache = tools
                logger.info("mcp_tools_discovered", server=url, count=len(tools))
                return tools
        except httpx.TimeoutException:
            logger.error("mcp_discovery_timeout", server=url)
            return []
        except Exception as exc:
            logger.error("mcp_discovery_failed", server=url, error=str(exc))
            return []

    async def call_tool(
        self, tool_name: str, arguments: dict[str, Any], server_url: str | None = None
    ) -> dict[str, Any]:
        """Call a tool on an MCP server."""
        url = server_url or self._config.mcp_server_url
        if not url:
            raise ValueError("No MCP server URL configured")

        try:
            async with httpx.AsyncClient(timeout=self._config.mcp_request_timeout) as client:
                response = await client.post(
                    f"{url.rstrip('/')}/tools/{tool_name}",
                    headers=self._build_headers(),
                    json={"arguments": arguments},
                )
                response.raise_for_status()
                return response.json()
        except httpx.TimeoutException:
            raise TimeoutError(f"MCP tool '{tool_name}' timed out")
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(f"MCP tool '{tool_name}' returned {exc.response.status_code}: {exc.response.text[:500]}")
        except Exception as exc:
            raise RuntimeError(f"MCP tool '{tool_name}' failed: {exc}")

    async def list_servers(self) -> list[dict[str, Any]]:
        """List available MCP servers (from config)."""
        servers = []
        if self._config.mcp_server_url:
            servers.append({
                "url": self._config.mcp_server_url,
                "enabled": self._config.mcp_enabled,
            })
        return servers

    def _build_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._config.mcp_api_key:
            headers["Authorization"] = f"Bearer {self._config.mcp_api_key}"
        return headers

    def clear_cache(self) -> None:
        self._tools_cache = None


# ── Singleton ─────────────────────────────────────────────────────────

_client: McpClient | None = None


def get_mcp_client() -> McpClient:
    """Get the singleton MCP client instance."""
    global _client
    if _client is None:
        _client = McpClient()
    return _client