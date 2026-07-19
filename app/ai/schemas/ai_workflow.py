"""AI Workflow schemas — Pydantic models for workflow definitions and executions."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class WorkflowStepDef(BaseModel):
    id: str
    type: str = Field(..., pattern="^(tool_call|llm_call|condition|transform|sub_workflow|human_review)$")
    tool_name: str | None = None
    prompt: str | None = None
    condition: str | None = None
    transform: str | None = None
    sub_workflow_id: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: int = 120
    retry_on_failure: bool = True
    max_retries: int = 0


class WorkflowEdge(BaseModel):
    from_: str = Field(..., alias="from")
    to: str


class WorkflowDefinitionCreate(BaseModel):
    name: str = Field(..., max_length=100)
    description: str | None = None
    steps: list[WorkflowStepDef] = Field(..., min_length=1)
    edges: list[WorkflowEdge] = Field(default_factory=list)
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    timeout_minutes: int = Field(30, ge=1, le=1440)
    max_retries: int = Field(0, ge=0, le=10)
    metadata: dict[str, Any] | None = None


class WorkflowDefinitionUpdate(BaseModel):
    description: str | None = None
    steps: list[WorkflowStepDef] | None = None
    edges: list[WorkflowEdge] | None = None
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    timeout_minutes: int | None = Field(None, ge=1, le=1440)
    max_retries: int | None = Field(None, ge=0, le=10)
    is_active: bool | None = None
    metadata: dict[str, Any] | None = None


class WorkflowDefinitionResponse(BaseModel):
    id: str
    tenant_id: str
    name: str
    description: str | None
    version: int
    steps: list[dict[str, Any]]
    edges: list[dict[str, Any]]
    input_schema: dict[str, Any] | None
    output_schema: dict[str, Any] | None
    timeout_minutes: int
    max_retries: int
    is_active: bool
    metadata: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class WorkflowExecutionRequest(BaseModel):
    workflow_id: str
    input_data: dict[str, Any] = Field(default_factory=dict)
    priority: int = Field(0, ge=0, le=100)


class WorkflowExecutionResponse(BaseModel):
    id: str
    tenant_id: str
    workflow_id: str
    workflow_name: str
    status: str
    input_data: dict[str, Any] | None
    output_data: dict[str, Any] | None
    error_message: str | None
    current_step: str | None
    progress_pct: float
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class WorkflowStepResponse(BaseModel):
    id: str
    execution_id: str
    step_id: str
    step_type: str
    status: str
    input_data: dict[str, Any] | None
    output_data: dict[str, Any] | None
    error_message: str | None
    duration_ms: int | None
    retry_count: int
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class WorkflowListResponse(BaseModel):
    workflows: list[WorkflowDefinitionResponse]
    total: int