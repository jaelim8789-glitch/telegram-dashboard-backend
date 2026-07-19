"""AI Plugin schemas — Pydantic models for plugin registration."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class PluginRegistrationCreate(BaseModel):
    name: str = Field(..., max_length=100)
    version: str = Field("1.0.0", max_length=20)
    description: str | None = None
    plugin_type: str = Field(..., pattern="^(tool_provider|workflow_step|event_handler|api_provider|custom)$")
    entry_point: str = Field(..., max_length=255)
    config_schema: dict[str, Any] | None = None
    config: dict[str, Any] | None = None
    provides_tools: list[str] = Field(default_factory=list)
    provides_workflow_steps: list[str] = Field(default_factory=list)
    provides_event_handlers: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] | None = None


class PluginRegistrationUpdate(BaseModel):
    version: str | None = None
    description: str | None = None
    config: dict[str, Any] | None = None
    is_active: bool | None = None
    metadata: dict[str, Any] | None = None


class PluginRegistrationResponse(BaseModel):
    id: str
    tenant_id: str
    name: str
    version: str
    description: str | None
    plugin_type: str
    entry_point: str
    config_schema: dict[str, Any] | None
    config: dict[str, Any] | None
    provides_tools: list[str]
    provides_workflow_steps: list[str]
    provides_event_handlers: list[str]
    dependencies: list[str]
    is_active: bool
    is_system: bool
    metadata: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class PluginListResponse(BaseModel):
    plugins: list[PluginRegistrationResponse]
    total: int