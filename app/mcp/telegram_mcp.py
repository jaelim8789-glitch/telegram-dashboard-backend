"""Telegram MCP server — Proof of Concept (TeleMon AI Platform Phase 1).

This PoC wraps TeleMon's own Telegram-facing capabilities as MCP tools so that
the AI Operations Center (and later, autonomous agents) can read/drive Telegram
through the same gateway as Grafana.

The PoC is intentionally read-biased and reuses existing TeleMon services where
possible (telethon_pool for account sessions, telegram_bot_service for the
Bot-API surface, delivery_analytics for account performance). Mutating tools
(``send_message``) are present but flagged ``requires_approval=True`` so the
gateway forces an explicit human approval step before any external send.

When the Telegram surface is not configured, tools degrade gracefully and return
a clear "not configured" error rather than crashing the gateway.
"""

from __future__ import annotations

from typing import Any

from app.config import settings
from app.core.logging import get_logger
from app.mcp.base import MCPTool, MCPToolResult, MCPToolServer

logger = get_logger(__name__)


class TelegramMCPServer(MCPToolServer):
    server_id = "telegram"
    title = "Telegram MCP (PoC)"
    enabled = settings.telegram_mcp_enabled

    # ─── Tool catalog ──────────────────────────────────────────────────────

    def list_tools(self) -> list[MCPTool]:
        return [
            MCPTool(
                name="list_accounts",
                description="TeleMon에 등록된 텔레그램 계정 목록을 조회합니다 (세션 상태 포함).",
                input_schema={
                    "type": "object",
                    "properties": {
                        "tenant_id": {"type": "string", "description": "선택적 테넌트 필터"}
                    },
                },
            ),
            MCPTool(
                name="account_health",
                description="특정 계정의 발송 성공률/실패율 등 건강 상태를 조회합니다.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "account_id": {"type": "string", "description": "계정 ID"},
                        "days": {"type": "integer", "default": 7},
                    },
                    "required": ["account_id"],
                },
            ),
            MCPTool(
                name="send_message",
                description="지정한 채팅으로 텔레그램 메시지를 발송합니다. 외부 상태를 변경하므로 승인이 필요합니다.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "account_id": {"type": "string"},
                        "chat_id": {"type": "string"},
                        "text": {"type": "string"},
                    },
                    "required": ["account_id", "chat_id", "text"],
                },
                requires_approval=True,
            ),
        ]

    # ─── Tool implementations ──────────────────────────────────────────────

    async def _tool_list_accounts(self, tenant_id: str | None = None) -> MCPToolResult:
        try:
            from app.database import async_session_maker
            from app.models.account import Account
            from sqlalchemy import select

            async with async_session_maker() as db:
                q = select(Account)
                if tenant_id:
                    q = q.where(Account.tenant_id == tenant_id)
                rows = (await db.execute(q)).scalars().all()
                accounts = [
                    {
                        "account_id": a.account_id,
                        "phone": getattr(a, "phone", None),
                        "status": getattr(a, "status", None),
                        "is_active": getattr(a, "is_active", None),
                    }
                    for a in rows
                ]
            return MCPToolResult(ok=True, data={"count": len(accounts), "accounts": accounts})
        except Exception as exc:  # noqa: BLE001
            logger.error("telegram_mcp_list_accounts_failed", error=str(exc))
            return MCPToolResult(ok=False, error=str(exc))

    async def _tool_account_health(
        self, account_id: str, days: int = 7
    ) -> MCPToolResult:
        try:
            from app.api.deps import Identity
            from app.services.delivery_analytics import get_account_performance

            identity = Identity(kind="admin")
            perf = await get_account_performance(identity, days=days)
            filtered = [p for p in perf if getattr(p, "account_id", None) == account_id]
            return MCPToolResult(
                ok=True,
                data={
                    "account_id": account_id,
                    "window_days": days,
                    "performance": [p.__dict__ for p in filtered] if filtered else None,
                    "note": "no rows" if not filtered else None,
                },
            )
        except Exception as exc:  # noqa: BLE001
            return MCPToolResult(ok=False, error=str(exc))

    async def _tool_send_message(
        self, account_id: str, chat_id: str, text: str
    ) -> MCPToolResult:
        # PoC: reaching this handler means the gateway already passed the
        # requires_approval gate. We confirm routing intent and defer the
        # actual external Telegram send to Phase 2 (real pool.send_message).
        # This keeps the PoC safe (no untested external sends) while proving
        # the full approval → execution path works end to end.
        try:
            from app.services.telethon_pool import pool

            connected = pool.peek_client(account_id) is not None
            return MCPToolResult(
                ok=True,
                data={
                    "routed": True,
                    "account_id": account_id,
                    "chat_id": chat_id,
                    "length": len(text),
                    "session_connected": connected,
                    "executed": False,
                    "note": "PoC: approval passed, external send deferred to Phase 2",
                },
            )
        except Exception as exc:  # noqa: BLE001
            return MCPToolResult(ok=False, error=str(exc))
