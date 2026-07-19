"""
AI Platform Configuration — settings for the entire AI Assistant Platform.

All AI platform settings are loaded from environment variables prefixed with AI_PLATFORM_.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.config import settings as app_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass
class AiPlatformConfig:
    """AI Platform configuration — singleton loaded once at startup."""

    # ── LLM Provider ──────────────────────────────────────────────────
    llm_provider: str = "deepseek"  # deepseek | openai | anthropic | custom
    llm_api_key: str = ""
    llm_api_base: str = "https://api.deepseek.com/v1"
    llm_model: str = "deepseek-chat"
    llm_max_tokens: int = 4096
    llm_temperature: float = 0.7
    llm_timeout_seconds: int = 120

    # ── MCP Tool Integration ──────────────────────────────────────────
    mcp_enabled: bool = False
    mcp_server_url: str = ""
    mcp_api_key: str = ""
    mcp_max_tools: int = 50
    mcp_request_timeout: int = 30

    # ── Task Queue ────────────────────────────────────────────────────
    task_queue_backend: str = "database"  # database | redis
    task_queue_redis_url: str = ""
    task_queue_max_retries: int = 3
    task_queue_retry_delay_seconds: int = 60
    task_queue_poll_interval: float = 1.0

    # ── Workflow Engine ───────────────────────────────────────────────
    workflow_max_steps: int = 50
    workflow_timeout_minutes: int = 30
    workflow_max_concurrent: int = 10

    # ── Event Bus ─────────────────────────────────────────────────────
    event_bus_backend: str = "inmemory"  # inmemory | redis | database
    event_bus_redis_url: str = ""
    event_bus_max_handlers_per_event: int = 20

    # ── Scheduler ─────────────────────────────────────────────────────
    scheduler_enabled: bool = True
    scheduler_tick_seconds: int = 15
    scheduler_max_jobs_per_tick: int = 50

    # ── Plugin System ─────────────────────────────────────────────────
    plugin_discovery_paths: list[str] = field(default_factory=lambda: ["app/ai/plugins"])
    plugin_auto_load: bool = True

    # ── API Integration ───────────────────────────────────────────────
    api_providers: dict[str, dict[str, Any]] = field(default_factory=dict)
    api_default_provider: str = "deepseek"
    api_cache_ttl_seconds: int = 300

    # ── Observability ─────────────────────────────────────────────────
    enable_metrics: bool = True
    enable_audit_log: bool = True
    log_payloads: bool = False  # Never enable in production

    @classmethod
    def from_settings(cls) -> AiPlatformConfig:
        """Load configuration from environment/app settings."""
        return cls(
            llm_api_key=app_settings.deepseek_api_key,
            llm_api_base=app_settings.deepseek_api_base,
            llm_model=app_settings.deepseek_model or "deepseek-chat",
            mcp_enabled=False,
            task_queue_backend="database",
            event_bus_backend="inmemory",
            scheduler_enabled=True,
        )


_config: AiPlatformConfig | None = None


def get_ai_config() -> AiPlatformConfig:
    """Get the singleton AI platform configuration."""
    global _config
    if _config is None:
        _config = AiPlatformConfig.from_settings()
        logger.info("ai_platform_config_loaded", provider=_config.llm_provider)
    return _config


def reload_ai_config() -> AiPlatformConfig:
    """Force-reload configuration (useful after env var changes)."""
    global _config
    _config = AiPlatformConfig.from_settings()
    logger.info("ai_platform_config_reloaded")
    return _config