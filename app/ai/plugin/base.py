"""
AI Plugin Base — abstract base class for all AI platform plugins.

Plugins can provide:
- Tools (via ToolRegistry)
- Workflow step handlers
- Event handlers
- API provider integrations
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class AiPluginBase(ABC):
    """Base class for all AI platform plugins."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique plugin name."""
        ...

    @property
    @abstractmethod
    def version(self) -> str:
        """Plugin version string."""
        ...

    @property
    def description(self) -> str:
        """Human-readable description."""
        return ""

    @abstractmethod
    async def initialize(self, config: dict[str, Any]) -> None:
        """Initialize the plugin with configuration."""
        ...

    async def shutdown(self) -> None:
        """Clean up plugin resources."""
        ...

    def get_tools(self) -> list[dict[str, Any]]:
        """Return tool definitions this plugin provides."""
        return []

    def get_event_handlers(self) -> dict[str, Any]:
        """Return event handlers this plugin provides.

        Returns dict mapping event_type -> handler function.
        """
        return {}

    def get_workflow_step_handlers(self) -> dict[str, Any]:
        """Return custom workflow step handlers.

        Returns dict mapping step_type -> handler function.
        """
        return {}