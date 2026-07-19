"""
AI Plugin Models — plugin registration and metadata.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AiPluginRegistration(Base):
    """AI Plugin Registration — registered plugins in the AI platform."""

    __tablename__ = "ai_plugin_registrations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    version: Mapped[str] = mapped_column(String(20), nullable=False, default="1.0.0")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    plugin_type: Mapped[str] = mapped_column(String(50), nullable=False)
    # tool_provider | workflow_step | event_handler | api_provider | custom
    entry_point: Mapped[str] = mapped_column(String(255), nullable=False)
    # Python dotted path to plugin class
    config_schema: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=dict)
    # JSON Schema for plugin configuration
    config: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=dict)
    # Current plugin configuration
    provides_tools: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)
    # List of tool names this plugin provides
    provides_workflow_steps: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)
    provides_event_handlers: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)
    dependencies: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)
    # List of other plugin names this depends on
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)
    meta: Mapped[dict | None] = mapped_column("metadata", JSON, nullable=True, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())