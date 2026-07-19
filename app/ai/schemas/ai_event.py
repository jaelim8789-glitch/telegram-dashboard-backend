"""AI Event schemas — Pydantic models for event bus."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class EventSubscriptionCreate(BaseModel):
    name: str = Field(..., max_length=100)
    event_type: str = Field(..., max_length=100)
    handler_type: str = Field("function", pattern="^(function|webhook|plugin|workflow)$")
    handler_ref: str = Field(..., max_length=255)
    filter_condition: dict[str, Any] | None = None
    retry_on_failure: bool = True
    max_retries: int = Field(3, ge=0, le=20)
    metadata: dict[str, Any] | None = None


class EventSubscriptionUpdate(BaseModel):
    name: str | None = None
    handler_ref: str | None = None
    filter_condition: dict[str, Any] | None = None
    is_active: bool | None = None
    retry_on_failure: bool | None = None
    max_retries: int | None = Field(None, ge=0, le=20)
    metadata: dict[str, Any] | None = None


class EventSubscriptionResponse(BaseModel):
    id: str
    tenant_id: str
    name: str
    event_type: str
    handler_type: str
    handler_ref: str
    filter_condition: dict[str, Any] | None
    is_active: bool
    retry_on_failure: bool
    max_retries: int
    metadata: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class EventPublishRequest(BaseModel):
    event_type: str = Field(..., max_length=100)
    payload: dict[str, Any] = Field(default_factory=dict)
    source: str = Field("api", max_length=100)
    correlation_id: str | None = None


class EventLogResponse(BaseModel):
    id: str
    tenant_id: str
    event_type: str
    source: str
    payload: dict[str, Any]
    correlation_id: str | None
    handler_count: int
    handler_success_count: int
    handler_failure_count: int
    created_at: datetime

    model_config = {"from_attributes": True}


class EventBusStats(BaseModel):
    total_events_24h: int = 0
    total_subscriptions: int = 0
    active_subscriptions: int = 0
    events_by_type: dict[str, int] = Field(default_factory=dict)
    failure_rate_24h: float = 0.0