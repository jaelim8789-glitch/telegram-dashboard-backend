"""AI Plugin System — extensible plugin architecture for AI platform."""

from app.ai.plugin.manager import PluginManager, get_plugin_manager
from app.ai.plugin.base import AiPluginBase

__all__ = ["PluginManager", "get_plugin_manager", "AiPluginBase"]