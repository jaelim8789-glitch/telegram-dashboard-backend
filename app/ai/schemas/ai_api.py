"""AI API schemas — Pydantic models for external API integration."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ApiProviderConfigCreate(BaseModel):
    provider_name: str = Field(..., max_length=50)
    api_base_url: str = Field(..., max_length=255)
    api_key: str = Field(..., max_length=500)
    model: str = Field(..., max_length=100)
    max_tokens: int = Field(4096, ge=1, le=128000)
    temperature: float = Field(0.7, ge=0.0, le=2.0)
    timeout_seconds: int = Field(120, ge=1, le=600)
    rate_limit_rpm: int = Field(60, ge=1)
    rate_limit_tpm: int = Field(100000, ge=1)
    is_default: bool = False
    meta: dict[str, Any] | None = None


class ApiProviderConfigUpdate(BaseModel):
    api_base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    max_tokens: int | None = Field(None, ge=1, le=128000)
    temperature: float | None = Field(None, ge=0.0, le=2.0)
    timeout_seconds: int | None = Field(None, ge=1, le=600)
    rate_limit_rpm: int | None = Field(None, ge=1)
    rate_limit_tpm: int | None = Field(None, ge=1)
    is_active: bool | None = None
    is_default: bool | None = None
    meta: dict[str, Any] | None = None


class ApiProviderConfigResponse(BaseModel):
    id: str
    tenant_id: str
    provider_name: str
    api_base_url: str
    model: str
    max_tokens: int
    temperature: float
    timeout_seconds: int
    rate_limit_rpm: int
    rate_limit_tpm: int
    is_active: bool
    is_default: bool
    metadata: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ApiCallRequest(BaseModel):
    provider: str = Field("deepseek", max_length=50)
    model: str | None = None
    messages: list[dict[str, Any]] = Field(..., min_length=1)
    max_tokens: int | None = None
    temperature: float | None = None
    tools: list[dict[str, Any]] | None = None
    stream: bool = False
    correlation_id: str | None = None


class ApiCallResponse(BaseModel):
    id: str
    provider: str
    model: str
    content: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    finish_reason: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    duration_ms: int | None = None


class ApiCallLogResponse(BaseModel):
    id: str
    tenant_id: str
    provider: str
    model: str
    endpoint: str
    status_code: int | None
    status: str
    error_message: str | None
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    duration_ms: int | None
    correlation_id: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ApiProviderListResponse(BaseModel):
    providers: list[ApiProviderConfigResponse]
    total: int