"""MCP Gateway — central registry and dispatcher (TeleMon AI Platform Phase 1).

The gateway is the single entry point that the AI Operations Center (and the
LangGraph supervisor) use to discover and invoke MCP tools across all registered
MCP servers (Telegram PoC, Grafana, ...). It:

- holds a process-local registry of ``MCPToolServer`` instances,
- exposes a catalog (server + tools) for the frontend to render,
- routes ``invoke_server_tool`` calls to the right server,
- enforces an approval gate for tools flagged ``requires_approval=True``.

It is intentionally transport-agnostic and dependency-free so it works in the
single-container production image without extra services.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass
class PendingApproval:
    server_id: str
    tool_name: str
    arguments: dict[str, Any]
    request_id: str


@dataclass
class GatewayCatalog:
    enabled: bool
    servers: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "servers": self.servers}


class MCPGateway:
    """Process-local registry of MCP servers and their dispatcher."""

    def __init__(self) -> None:
        self._servers: dict[str, Any] = {}
        self._pending: dict[str, PendingApproval] = {}
        self._register_defaults()

    def _register_defaults(self) -> None:
        from app.mcp.grafana_mcp import GrafanaMCPServer
        from app.mcp.telegram_mcp import TelegramMCPServer

        self.register(TelegramMCPServer())
        self.register(GrafanaMCPServer())

    # ─── Registry ──────────────────────────────────────────────────────────

    def register(self, server: Any) -> None:
        self._servers[server.server_id] = server
        logger.info("mcp_server_registered", server=server.server_id, enabled=server.enabled)

    def get(self, server_id: str) -> Any | None:
        return self._servers.get(server_id)

    @property
    def enabled(self) -> bool:
        return settings.mcp_gateway_enabled

    def catalog(self) -> GatewayCatalog:
        return GatewayCatalog(
            enabled=self.enabled,
            servers=[s.to_catalog_entry() for s in self._servers.values()],
        )

    # ─── Dispatch ──────────────────────────────────────────────────────────

    async def invoke(
        self,
        server_id: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        approved: bool = False,
    ) -> dict[str, Any]:
        """Invoke a tool on a registered server.

        Tools with ``requires_approval=True`` are not executed on the first call.
        Instead a ``pending_approval`` result is returned with a ``request_id``.
        The caller must re-invoke with the same request_id and ``approved=True``
        (after the human approves) to actually run the tool.
        """
        if not self.enabled:
            return {"ok": False, "error": "mcp_gateway_disabled"}

        server = self._servers.get(server_id)
        if server is None:
            return {"ok": False, "error": f"unknown server '{server_id}'"}
        if not server.enabled:
            return {"ok": False, "error": f"server '{server_id}' not enabled"}

        tool = next((t for t in server.list_tools() if t.name == tool_name), None)
        if tool is None:
            return {"ok": False, "error": f"unknown tool '{tool_name}' on server '{server_id}'"}

        if tool.requires_approval and not approved:
            req_id = f"{server_id}:{tool_name}:{len(self._pending)}"
            self._pending[req_id] = PendingApproval(
                server_id=server_id,
                tool_name=tool_name,
                arguments=arguments or {},
                request_id=req_id,
            )
            return {
                "ok": False,
                "pending_approval": True,
                "request_id": req_id,
                "server_id": server_id,
                "tool_name": tool_name,
                "message": "이 도구는 외부 상태를 변경하므로 승인이 필요합니다.",
            }

        result = await server.invoke(tool_name, arguments or {})
        return result.to_dict()

    def get_pending(self, request_id: str) -> PendingApproval | None:
        return self._pending.get(request_id)

    def clear_pending(self, request_id: str) -> None:
        self._pending.pop(request_id, None)


# Module-level singleton — shared across requests in the running app.
gateway = MCPGateway()
