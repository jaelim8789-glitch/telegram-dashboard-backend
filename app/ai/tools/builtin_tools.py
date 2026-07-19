"""
Built-in Tools — default tools registered at startup for the AI platform.

These provide core TeleMon operations that the AI can invoke.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from app.ai.tools.registry import get_tool_registry
from app.core.logging import get_logger

logger = get_logger(__name__)


# ── Built-in Tool Handlers ────────────────────────────────────────────


async def tool_get_current_time(**kwargs: Any) -> dict[str, Any]:
    """Get the current date and time."""
    now = datetime.now(timezone.utc)
    return {
        "utc_iso": now.isoformat(),
        "utc_timestamp": now.timestamp(),
        "timezone": "UTC",
    }


async def tool_echo(**kwargs: Any) -> dict[str, Any]:
    """Echo back the input (useful for testing)."""
    return {"echo": kwargs.get("message", "")}


async def tool_calculate(**kwargs: Any) -> dict[str, Any]:
    """Perform a basic calculation."""
    expression = kwargs.get("expression", "")
    try:
        # Safe evaluation — only allow basic math
        allowed_names = {
            "abs": abs, "round": round, "min": min, "max": max,
            "sum": sum, "int": int, "float": float, "str": str,
        }
        result = eval(expression, {"__builtins__": {}}, allowed_names)
        return {"expression": expression, "result": result}
    except Exception as exc:
        return {"expression": expression, "error": str(exc)}


async def tool_format_text(**kwargs: Any) -> dict[str, Any]:
    """Format text with specified transformation."""
    text = kwargs.get("text", "")
    style = kwargs.get("style", "uppercase")
    if style == "uppercase":
        return {"result": text.upper()}
    elif style == "lowercase":
        return {"result": text.lower()}
    elif style == "capitalize":
        return {"result": text.capitalize()}
    elif style == "title":
        return {"result": text.title()}
    elif style == "trim":
        return {"result": text.strip()}
    elif style == "reverse":
        return {"result": text[::-1]}
    return {"result": text}


# ── Tool Definitions ──────────────────────────────────────────────────

BUILTIN_TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_current_time",
        "description": "Get the current UTC date and time",
        "tool_type": "function",
        "source": "builtin",
        "parameters": {
            "type": "object",
            "properties": {},
        },
        "is_public": True,
        "timeout_seconds": 10,
    },
    {
        "name": "echo",
        "description": "Echo back a message (useful for testing tool calling)",
        "tool_type": "function",
        "source": "builtin",
        "parameters": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Message to echo back"}
            },
            "required": ["message"],
        },
        "is_public": True,
        "timeout_seconds": 10,
    },
    {
        "name": "calculate",
        "description": "Perform a basic mathematical calculation",
        "tool_type": "function",
        "source": "builtin",
        "parameters": {
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "Mathematical expression (e.g., '2 + 2', 'abs(-5)')",
                }
            },
            "required": ["expression"],
        },
        "is_public": True,
        "timeout_seconds": 10,
    },
    {
        "name": "format_text",
        "description": "Format or transform text (uppercase, lowercase, capitalize, title, trim, reverse)",
        "tool_type": "function",
        "source": "builtin",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to transform"},
                "style": {
                    "type": "string",
                    "enum": ["uppercase", "lowercase", "capitalize", "title", "trim", "reverse"],
                    "description": "Transformation style",
                },
            },
            "required": ["text", "style"],
        },
        "is_public": True,
        "timeout_seconds": 10,
    },
]


def register_builtin_tools() -> None:
    """Register all built-in tools with the tool registry."""
    registry = get_tool_registry()

    # Register handlers
    registry.register_handler("get_current_time", tool_get_current_time)
    registry.register_handler("echo", tool_echo)
    registry.register_handler("calculate", tool_calculate)
    registry.register_handler("format_text", tool_format_text)

    logger.info("builtin_tools_registered", count=len(BUILTIN_TOOLS))