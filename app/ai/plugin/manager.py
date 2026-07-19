"""
Plugin Manager — discovers, loads, and manages AI platform plugins.

Handles plugin lifecycle: discovery → validation → initialization → activation.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.config import get_ai_config
from app.ai.models.ai_plugin import AiPluginRegistration
from app.ai.plugin.base import AiPluginBase
from app.ai.tools.registry import get_tool_registry
from app.core.logging import get_logger

logger = get_logger(__name__)


class PluginManager:
    """Discovers, loads, and manages AI platform plugins."""

    def __init__(self) -> None:
        self._config = get_ai_config()
        self._registry = get_tool_registry()
        self._plugins: dict[str, AiPluginBase] = {}
        self._loaded = False

    # ── Plugin Lifecycle ─────────────────────────────────────────────

    async def discover_and_load(self) -> int:
        """Discover plugins from configured paths and load them.

        Returns the number of plugins loaded.
        """
        count = 0
        for path in self._config.plugin_discovery_paths:
            try:
                module = importlib.import_module(path.replace("/", "."))
                for _, name, is_pkg in pkgutil.iter_modules(module.__path__):
                    if not is_pkg:
                        continue
                    try:
                        plugin_module = importlib.import_module(f"{path.replace('/', '.')}.{name}")
                        await self._load_plugin_from_module(plugin_module)
                        count += 1
                    except Exception as exc:
                        logger.warning("plugin_load_failed", plugin=name, error=str(exc))
            except (ImportError, AttributeError) as exc:
                logger.debug("plugin_path_not_found", path=path, error=str(exc))

        self._loaded = True
        logger.info("plugins_discovered_and_loaded", count=count)
        return count

    async def load_plugin(self, plugin: AiPluginBase, config: dict[str, Any] | None = None) -> bool:
        """Load and initialize a single plugin."""
        if plugin.name in self._plugins:
            logger.warning("plugin_already_loaded", name=plugin.name)
            return False

        try:
            await plugin.initialize(config or {})
            self._plugins[plugin.name] = plugin

            # Register tools
            for tool_def in plugin.get_tools():
                self._registry.register_handler(tool_def["name"], tool_def.get("handler"))

            logger.info("plugin_loaded", name=plugin.name, version=plugin.version)
            return True
        except Exception as exc:
            logger.error("plugin_initialization_failed", name=plugin.name, error=str(exc))
            return False

    async def unload_plugin(self, name: str) -> bool:
        """Unload a plugin."""
        plugin = self._plugins.pop(name, None)
        if not plugin:
            return False
        try:
            await plugin.shutdown()
            logger.info("plugin_unloaded", name=name)
            return True
        except Exception as exc:
            logger.error("plugin_shutdown_failed", name=name, error=str(exc))
            return False

    async def load_all(self, plugins: list[AiPluginBase]) -> int:
        """Load multiple plugins at once."""
        count = 0
        for plugin in plugins:
            if await self.load_plugin(plugin):
                count += 1
        return count

    async def _load_plugin_from_module(self, module: Any) -> None:
        """Scan a module for AiPluginBase subclasses and load them."""
        for _, obj in inspect.getmembers(module):
            if (inspect.isclass(obj) and issubclass(obj, AiPluginBase) and obj is not AiPluginBase):
                plugin_instance = obj()
                await self.load_plugin(plugin_instance)

    # ── Accessors ────────────────────────────────────────────────────

    def get_plugin(self, name: str) -> AiPluginBase | None:
        """Get a loaded plugin by name."""
        return self._plugins.get(name)

    def list_plugins(self) -> list[AiPluginBase]:
        """List all loaded plugins."""
        return list(self._plugins.values())

    def has_plugin(self, name: str) -> bool:
        return name in self._plugins

    @property
    def is_loaded(self) -> bool:
        return self._loaded


# ── Singleton ─────────────────────────────────────────────────────────

_manager: PluginManager | None = None


def get_plugin_manager() -> PluginManager:
    """Get the singleton plugin manager instance."""
    global _manager
    if _manager is None:
        _manager = PluginManager()
    return _manager