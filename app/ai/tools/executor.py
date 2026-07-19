"""
Tool Executor — executes tool calls with retry, timeout, and logging.

Supports:
- Python function handlers (builtin/plugin)
- MCP tool calls (via MCP client)
- Webhook calls (HTTP POST)
- API calls (via API integration layer)
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.config import get_ai_config
from app.ai.models.ai_tool import AiToolExecutionLog
from app.ai.tools.registry import get_tool_registry
from app.core.logging import get_logger

logger = get_logger(__name__)


class ToolExecutor:
    """Executes tool calls with error isolation, retry, and audit logging."""

    def __init__(self) -> None:
        self._registry = get_tool_registry()

    async def execute(
        self,
        db: AsyncSession,
        tenant_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        session_id: str | None = None,
        workflow_execution_id: str | None = None,
        task_id: str | None = None,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Execute a tool call and return the result.

        Returns a dict with keys: execution_id, status, result, error_message, duration_ms, tokens_used
        """
        definition = self._registry.get_definition(tool_name)
        if not definition:
            return {
                "execution_id": str(uuid.uuid4()),
                "status": "error",
                "error_message": f"Tool '{tool_name}' not found",
                "duration_ms": 0,
                "tokens_used": 0,
            }

        if not definition.is_active:
            return {
                "execution_id": str(uuid.uuid4()),
                "status": "error",
                "error_message": f"Tool '{tool_name}' is inactive",
                "duration_ms": 0,
                "tokens_used": 0,
            }

        timeout = timeout_seconds or definition.timeout_seconds
        max_retries = definition.max_retries
        execution_id = str(uuid.uuid4())

        # Create execution log
        log_entry = AiToolExecutionLog(
            id=execution_id,
            tenant_id=tenant_id,
            tool_name=tool_name,
            session_id=session_id,
            workflow_execution_id=workflow_execution_id,
            task_id=task_id,
            arguments=arguments,
            status="running",
        )
        db.add(log_entry)
        await db.commit()

        start_time = time.monotonic()
        last_error: str | None = None

        for attempt in range(max_retries + 1):
            try:
                result = await asyncio.wait_for(
                    self._call_tool(definition, arguments),
                    timeout=timeout,
                )

                duration_ms = int((time.monotonic() - start_time) * 1000)
                tokens_used = result.get("tokens_used", 0)

                # Update log
                log_entry.status = "success"
                log_entry.result = result.get("data")
                log_entry.duration_ms = duration_ms
                log_entry.tokens_used = tokens_used
                log_entry.completed_at = __import__("datetime").datetime.now()
                await db.commit()

                logger.info(
                    "tool_executed",
                    tool_name=tool_name,
                    duration_ms=duration_ms,
                    attempt=attempt + 1,
                )

                return {
                    "execution_id": execution_id,
                    "status": "success",
                    "result": result.get("data"),
                    "error_message": None,
                    "duration_ms": duration_ms,
                    "tokens_used": tokens_used,
                }

            except asyncio.TimeoutError:
                last_error = f"Tool '{tool_name}' timed out after {timeout}s"
                logger.warning("tool_timeout", tool_name=tool_name, timeout=timeout, attempt=attempt + 1)
                if attempt < max_retries:
                    await asyncio.sleep(1 * (attempt + 1))  # Exponential backoff
                    continue
                break

            except Exception as exc:
                last_error = str(exc)
                logger.error("tool_execution_failed", tool_name=tool_name, error=last_error, attempt=attempt + 1)
                if attempt < max_retries:
                    await asyncio.sleep(1 * (attempt + 1))
                    continue
                break

        # All retries exhausted
        duration_ms = int((time.monotonic() - start_time) * 1000)
        log_entry.status = "error"
        log_entry.error_message = last_error
        log_entry.duration_ms = duration_ms
        log_entry.retry_count = max_retries
        log_entry.completed_at = __import__("datetime").datetime.now()
        await db.commit()

        return {
            "execution_id": execution_id,
            "status": "error",
            "result": None,
            "error_message": last_error,
            "duration_ms": duration_ms,
            "tokens_used": 0,
        }

    async def _call_tool(
        self, definition: "AiToolDefinition", arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Route the tool call to the appropriate handler."""
        tool_type = definition.tool_type
        handler_ref = definition.handler_ref

        if tool_type == "function":
            handler = self._registry.get_handler(definition.name)
            if handler is None:
                # Try to import from handler_ref
                if handler_ref:
                    handler = self._import_handler(handler_ref)
                if handler is None:
                    raise ValueError(f"No handler registered for tool '{definition.name}'")
            result = await handler(**arguments) if asyncio.iscoroutinefunction(handler) else handler(**arguments)
            return {"data": result if isinstance(result, dict) else {"result": result}}

        elif tool_type == "mcp":
            # MCP tool call — delegate to MCP client
            from app.ai.tools.mcp_client import get_mcp_client
            client = get_mcp_client()
            result = await client.call_tool(handler_ref or definition.name, arguments)
            return {"data": result}

        elif tool_type == "api":
            # External API call
            import httpx
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    handler_ref or "",
                    json=arguments,
                    headers={"Content-Type": "application/json"},
                )
                response.raise_for_status()
                return {"data": response.json()}

        elif tool_type == "webhook":
            # Webhook call
            import httpx
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    handler_ref or "",
                    json=arguments,
                )
                return {"data": {"status_code": response.status_code, "body": response.text}}

        else:
            raise ValueError(f"Unknown tool type: {tool_type}")

    def _import_handler(self, dotted_path: str) -> Any:
        """Import a Python function from a dotted path string."""
        import importlib
        module_path, _, func_name = dotted_path.rpartition(".")
        module = importlib.import_module(module_path)
        return getattr(module, func_name, None)


# ── Singleton ─────────────────────────────────────────────────────────

_executor: ToolExecutor | None = None


def get_tool_executor() -> ToolExecutor:
    """Get the singleton tool executor instance."""
    global _executor
    if _executor is None:
        _executor = ToolExecutor()
    return _executor