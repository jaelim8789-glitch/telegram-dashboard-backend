"""Grafana MCP server (TeleMon AI Platform Phase 1).

Exposes Grafana datasource queries as MCP tools. It talks to the Grafana HTTP
API (``/api/datasources/proxy`` for Prometheus, ``/api/ds/query`` for the
unified query endpoint). When ``grafana_base_url`` / ``grafana_api_token`` are
not configured the server stays ``enabled=False`` and every tool returns a
clear "not configured" result — never crashing the gateway.

Tools are read-only (Prometheus instant/range queries, Loki log queries), so
they do not require approval.
"""

from __future__ import annotations

import httpx
from typing import Any

from app.config import settings
from app.core.logging import get_logger
from app.mcp.base import MCPTool, MCPToolResult, MCPToolServer

logger = get_logger(__name__)

_REQUEST_TIMEOUT = 15.0


class GrafanaMCPServer(MCPToolServer):
    server_id = "grafana"
    title = "Grafana MCP"
    enabled = settings.grafana_mcp_enabled

    # ─── Tool catalog ──────────────────────────────────────────────────────

    def list_tools(self) -> list[MCPTool]:
        return [
            MCPTool(
                name="query_prometheus",
                description="Grafana의 Prometheus datasource에 대해 PromQL instant query를 실행합니다.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "PromQL 표현식"},
                        "time": {"type": "string", "description": "ISO8601 또는 Unix timestamp (선택)"},
                    },
                    "required": ["query"],
                },
            ),
            MCPTool(
                name="query_range",
                description="Prometheus range query를 실행하여 시계열 데이터를 조회합니다.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "start": {"type": "string"},
                        "end": {"type": "string"},
                        "step": {"type": "string", "default": "60s"},
                    },
                    "required": ["query", "start", "end"],
                },
            ),
            MCPTool(
                name="query_logs",
                description="Loki datasource에 대해 LogQL 쿼리를 실행하여 로그를 조회합니다.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "LogQL 표현식 (예: {app=\"telemon\"})"},
                        "limit": {"type": "integer", "default": 100},
                    },
                    "required": ["query"],
                },
            ),
            MCPTool(
                name="list_dashboards",
                description="Grafana에 등록된 대시보드 목록을 조회합니다.",
                input_schema={"type": "object", "properties": {}},
            ),
        ]

    # ─── Internal helpers ──────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {settings.grafana_api_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _configured(self) -> bool:
        return bool(settings.grafana_base_url and settings.grafana_api_token)

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        if not self._configured():
            return None
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.grafana_base_url}{path}",
                headers=self._headers(),
                json=payload,
            )
            resp.raise_for_status()
            return resp.json()

    # ─── Tool implementations ──────────────────────────────────────────────

    async def _tool_query_prometheus(
        self, query: str, time: str | None = None
    ) -> MCPToolResult:
        payload: dict[str, Any] = {
            "queries": [
                {
                    "refId": "A",
                    "expr": query,
                    "datasource": {"uid": settings.grafana_datasource_uid},
                }
            ],
            "from": time or "now",
            "to": "now",
        }
        try:
            data = await self._post("/api/ds/query", payload)
        except httpx.HTTPError as exc:
            return MCPToolResult(ok=False, error=f"grafana request failed: {exc}")
        if data is None:
            return MCPToolResult(ok=False, error="grafana not configured")
        return MCPToolResult(ok=True, data=data)

    async def _tool_query_range(
        self, query: str, start: str, end: str, step: str = "60s"
    ) -> MCPToolResult:
        payload = {
            "queries": [
                {
                    "refId": "A",
                    "expr": query,
                    "range": True,
                    "step": step,
                    "datasource": {"uid": settings.grafana_datasource_uid},
                }
            ],
            "from": start,
            "to": end,
        }
        try:
            data = await self._post("/api/ds/query", payload)
        except httpx.HTTPError as exc:
            return MCPToolResult(ok=False, error=f"grafana request failed: {exc}")
        if data is None:
            return MCPToolResult(ok=False, error="grafana not configured")
        return MCPToolResult(ok=True, data=data)

    async def _tool_query_logs(self, query: str, limit: int = 100) -> MCPToolResult:
        payload = {
            "queries": [
                {
                    "refId": "A",
                    "expr": query,
                    "queryType": "range",
                    "maxLines": limit,
                    "datasource": {"uid": settings.grafana_datasource_uid, "type": "loki"},
                }
            ],
            "from": "now-1h",
            "to": "now",
        }
        try:
            data = await self._post("/api/ds/query", payload)
        except httpx.HTTPError as exc:
            return MCPToolResult(ok=False, error=f"grafana request failed: {exc}")
        if data is None:
            return MCPToolResult(ok=False, error="grafana not configured")
        return MCPToolResult(ok=True, data=data)

    async def _tool_list_dashboards(self) -> MCPToolResult:
        if not self._configured():
            return MCPToolResult(ok=False, error="grafana not configured")
        try:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                resp = await client.get(
                    f"{settings.grafana_base_url}/api/search?type=dash-db",
                    headers=self._headers(),
                )
                resp.raise_for_status()
                return MCPToolResult(ok=True, data={"dashboards": resp.json()})
        except httpx.HTTPError as exc:
            return MCPToolResult(ok=False, error=f"grafana request failed: {exc}")
