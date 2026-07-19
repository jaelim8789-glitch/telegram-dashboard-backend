"""MCP server base contract for TeleMon (Phase 1).

An ``MCPToolServer`` exposes a discoverable set of tools (name, description,
input schema) and an ``invoke`` method. This mirrors the Model Context Protocol
"server" role closely enough for TeleMon's gateway to proxy tool calls, while
remaining a plain Python object (no network transport required for the PoC).

Later phases can subclass these into real MCP-over-stdio / SSE servers; the
gateway depends only on this interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class MCPTool:
    """A single callable tool exposed by an MCP server."""

    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})
    # Optional flag — tools that mutate external state should require an
    # explicit approval gate when invoked through the gateway.
    requires_approval: bool = False


@dataclass
class MCPToolResult:
    """Normalized result returned from a tool invocation."""

    ok: bool
    data: Any = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "data": self.data, "error": self.error}


class MCPToolServer(ABC):
    """Base class for every MCP server registered in the gateway."""

    #: Stable server id used in the registry (e.g. "telegram", "grafana").
    server_id: str = "base"
    #: Human-readable title shown in the Operations Center.
    title: str = "Base MCP Server"
    #: When False the gateway reports the server as unavailable (not configured).
    enabled: bool = False

    @abstractmethod
    def list_tools(self) -> list[MCPTool]:
        """Return the tools this server exposes."""

    async def invoke(self, tool_name: str, arguments: dict[str, Any]) -> MCPToolResult:
        """Dispatch a tool call. Default routes to a ``_tool_<name>`` coroutine."""
        handler = getattr(self, f"_tool_{tool_name}", None)
        if handler is None:
            return MCPToolResult(ok=False, error=f"unknown tool '{tool_name}'")
        try:
            result = handler(**(arguments or {}))
            if hasattr(result, "__await__"):
                result = await result
            if isinstance(result, MCPToolResult):
                return result
            return MCPToolResult(ok=True, data=result)
        except TypeError as exc:
            return MCPToolResult(ok=False, error=f"invalid arguments: {exc}")
        except Exception as exc:  # noqa: BLE001
            return MCPToolResult(ok=False, error=str(exc))

    def to_catalog_entry(self) -> dict[str, Any]:
        return {
            "server_id": self.server_id,
            "title": self.title,
            "enabled": self.enabled,
            "tools": [t.__dict__ for t in self.list_tools()],
        }
