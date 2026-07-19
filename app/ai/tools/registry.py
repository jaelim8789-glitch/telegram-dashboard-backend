"""
Tool Registry — central registry for all AI-callable tools.

Tools can be registered from:
- Built-in Python functions
- MCP server tools
- Plugin-provided tools
- Custom handler references
"""

from __future__ import annotations

import uuid
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.config import get_ai_config
from app.ai.models.ai_tool import AiToolDefinition
from app.core.logging import get_logger

logger = get_logger(__name__)

# Type alias for tool handler functions
ToolHandler = Callable[..., Any]


class ToolRegistry:
    """Central registry for AI-callable tools.

    Maintains both an in-memory cache of active tools and a DB-backed
    registry for persistence across restarts.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, ToolHandler] = {}
        self._definitions: dict[str, AiToolDefinition] = {}
        self._loaded = False

    # ── Registration ──────────────────────────────────────────────────

    def register_handler(self, name: str, handler: ToolHandler) -> None:
        """Register a Python function as a tool handler."""
        self._handlers[name] = handler
        logger.debug("tool_handler_registered", name=name)

    def unregister_handler(self, name: str) -> None:
        """Remove a registered handler."""
        self._handlers.pop(name, None)
        logger.debug("tool_handler_unregistered", name=name)

    def get_handler(self, name: str) -> ToolHandler | None:
        """Get a registered handler by name."""
        return self._handlers.get(name)

    def has_handler(self, name: str) -> bool:
        return name in self._handlers

    # ── DB-backed definitions ─────────────────────────────────────────

    async def load_from_db(self, db: AsyncSession) -> int:
        """Load all active tool definitions from the database."""
        result = await db.execute(
            select(AiToolDefinition).where(AiToolDefinition.is_active == True)
        )
        tools = result.scalars().all()
        for tool in tools:
            self._definitions[tool.name] = tool
        self._loaded = True
        logger.info("tools_loaded_from_db", count=len(tools))
        return len(tools)

    async def create_definition(
        self, db: AsyncSession, tenant_id: str, data: dict[str, Any]
    ) -> AiToolDefinition:
        """Create a new tool definition in the database."""
        tool = AiToolDefinition(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            **{k: v for k, v in data.items() if hasattr(AiToolDefinition, k)},
        )
        db.add(tool)
        await db.commit()
        await db.refresh(tool)
        self._definitions[tool.name] = tool
        logger.info("tool_definition_created", name=tool.name, tenant_id=tenant_id)
        return tool

    async def update_definition(
        self, db: AsyncSession, tool_id: str, data: dict[str, Any]
    ) -> AiToolDefinition | None:
        """Update an existing tool definition."""
        result = await db.execute(
            select(AiToolDefinition).where(AiToolDefinition.id == tool_id)
        )
        tool = result.scalar_one_or_none()
        if not tool:
            return None
        for key, value in data.items():
            if hasattr(tool, key) and value is not None:
                setattr(tool, key, value)
        await db.commit()
        await db.refresh(tool)
        self._definitions[tool.name] = tool
        logger.info("tool_definition_updated", name=tool.name)
        return tool

    async def delete_definition(self, db: AsyncSession, tool_id: str) -> bool:
        """Delete a tool definition."""
        result = await db.execute(
            select(AiToolDefinition).where(AiToolDefinition.id == tool_id)
        )
        tool = result.scalar_one_or_none()
        if not tool:
            return False
        self._definitions.pop(tool.name, None)
        await db.delete(tool)
        await db.commit()
        logger.info("tool_definition_deleted", name=tool.name)
        return True

    def get_definition(self, name: str) -> AiToolDefinition | None:
        """Get a tool definition by name."""
        return self._definitions.get(name)

    def list_definitions(self) -> list[AiToolDefinition]:
        """List all loaded tool definitions."""
        return list(self._definitions.values())

    def get_openai_tools_schema(self) -> list[dict[str, Any]]:
        """Generate OpenAI-compatible tool schema for LLM function calling.

        Returns a list of tool definitions formatted for OpenAI/DeepSeek
        function calling API.
        """
        tools = []
        for tool in self._definitions.values():
            if not tool.is_active:
                continue
            tools.append({
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters or {"type": "object", "properties": {}},
                },
            })
        return tools

    @property
    def is_loaded(self) -> bool:
        return self._loaded


# ── Singleton ─────────────────────────────────────────────────────────

_registry: ToolRegistry | None = None


def get_tool_registry() -> ToolRegistry:
    """Get the singleton tool registry instance."""
    global _registry
    if _registry is None:
        _registry = ToolRegistry()
    return _registry