"""AI Task schemas — Pydantic models for task queue."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class TaskCreate(BaseModel):
    task_type: str = Field(..., max_length=100)
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: int = Field(0, ge=0, le=100)
    max_retries: int = Field(3, ge=0, le=20)
    schedule_at: datetime | None = None
    session_id: str | None = None
    workflow_execution_id: str | None = None


class TaskResponse(BaseModel):
    id: str
    tenant_id: str
    task_type: str
    priority: int
    status: str
    payload: dict[str, Any]
    result: dict[str, Any] | None
    error_message: str | None
    retry_count: int
    max_retries: int
    schedule_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class TaskLogResponse(BaseModel):
    id: str
    task_id: str
    tenant_id: str
    level: str
    message: str
    details: dict[str, Any] | None
    created_at: datetime

    model_config = {"from_attributes": True}


class TaskListResponse(BaseModel):
    tasks: list[TaskResponse]
    total: int
    pending: int
    running: int
    completed: int
    failed: int


class TaskQueueStats(BaseModel):
    total_pending: int
    total_running: int
    total_completed: int
    total_failed: int
    total_cancelled: int
    avg_completion_time_ms: float = 0.0
    oldest_pending_minutes: float = 0.0