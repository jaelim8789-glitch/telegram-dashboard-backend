"""AI Schedule schemas — Pydantic models for scheduled jobs."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ScheduleDefinitionCreate(BaseModel):
    name: str = Field(..., max_length=100)
    description: str | None = None
    action_type: str = Field(..., pattern="^(tool_call|workflow_run|task_enqueue|event_emit|plugin_action|api_call)$")
    action_config: dict[str, Any] = Field(default_factory=dict)
    schedule_type: str = Field(..., pattern="^(interval|cron|once)$")
    interval_seconds: int | None = Field(None, ge=1)
    cron_expression: str | None = None
    run_at: datetime | None = None
    timezone: str = "Asia/Seoul"
    max_executions: int = Field(0, ge=0)
    metadata: dict[str, Any] | None = None


class ScheduleDefinitionUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    action_config: dict[str, Any] | None = None
    interval_seconds: int | None = Field(None, ge=1)
    cron_expression: str | None = None
    run_at: datetime | None = None
    timezone: str | None = None
    max_executions: int | None = Field(None, ge=0)
    is_active: bool | None = None
    metadata: dict[str, Any] | None = None


class ScheduleDefinitionResponse(BaseModel):
    id: str
    tenant_id: str
    name: str
    description: str | None
    action_type: str
    action_config: dict[str, Any]
    schedule_type: str
    interval_seconds: int | None
    cron_expression: str | None
    run_at: datetime | None
    timezone: str
    max_executions: int
    current_executions: int
    is_active: bool
    metadata: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ScheduleExecutionResponse(BaseModel):
    id: str
    schedule_id: str
    tenant_id: str
    status: str
    result: dict[str, Any] | None
    error_message: str | None
    duration_ms: int | None
    triggered_at: datetime
    completed_at: datetime | None

    model_config = {"from_attributes": True}


class ScheduleListResponse(BaseModel):
    schedules: list[ScheduleDefinitionResponse]
    total: int