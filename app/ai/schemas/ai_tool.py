"""AI Tool schemas — Pydantic models for tool definitions and execution."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ToolDefinitionCreate(BaseModel):
    name: str = Field(..., max_length=100, description="Unique tool name")
    description: str = Field(..., description="Tool description for LLM")
    tool_type: str = Field("function", pattern="^(function|mcp|api|webhook)$")
    source: str = Field("builtin", pattern="^(builtin|mcp|plugin|custom)$")
    parameters: dict[str, Any] = Field(default_factory=dict, description="JSON Schema")
    handler_ref: str | None = Field(None, max_length=255)
    is_public: bool = False
    timeout_seconds: int = Field(30, ge=1, le=300)
    max_retries: int = Field(0, ge=0, le=10)
    metadata: dict[str, Any] | None = None


class ToolDefinitionUpdate(BaseModel):
    description: str | None = None
    parameters: dict[str, Any] | None = None
    handler_ref: str | None = None
    is_active: bool | None = None
    is_public: bool | None = None
    timeout_seconds: int | None = Field(None, ge=1, le=300)
    max_retries: int | None = Field(None, ge=0, le=10)
    metadata: dict[str, Any] | None = None


class ToolDefinitionResponse(BaseModel):
    id: str
    tenant_id: str
    name: str
    description: str
    tool_type: str
    source: str
    parameters: dict[str, Any]
    handler_ref: str | None
    is_active: bool
    is_public: bool
    timeout_seconds: int
    max_retries: int
    metadata: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ToolExecutionRequest(BaseModel):
    tool_name: str = Field(..., description="Name of the tool to execute")
    arguments: dict[str, Any] = Field(default_factory=dict)
    session_id: str | None = None
    workflow_execution_id: str | None = None
    task_id: str | None = None
    timeout_seconds: int | None = None


class ToolExecutionResponse(BaseModel):
    execution_id: str
    tool_name: str
    status: str
    result: dict[str, Any] | None = None
    error_message: str | None = None
    duration_ms: int | None = None
    tokens_used: int = 0


class ToolExecutionLogResponse(BaseModel):
    id: str
    tenant_id: str
    tool_name: str
    session_id: str | None
    workflow_execution_id: str | None
    task_id: str | None
    arguments: dict[str, Any]
    result: dict[str, Any] | None
    status: str
    error_message: str | None
    duration_ms: int | None
    tokens_used: int
    retry_count: int
    created_at: datetime
    completed_at: datetime | None

    model_config = {"from_attributes": True}


class ToolListResponse(BaseModel):
    tools: list[ToolDefinitionResponse]
    total: int