"""TeleMon MCP servers (Phase 1).

Exposes the MCP tool-server contract. Import concrete servers via their modules
(``app.mcp.telegram_mcp``, ``app.mcp.grafana_mcp``) or the gateway which
registers them automatically.
"""

from app.mcp.base import MCPTool, MCPToolResult, MCPToolServer

__all__ = ["MCPTool", "MCPToolResult", "MCPToolServer"]
