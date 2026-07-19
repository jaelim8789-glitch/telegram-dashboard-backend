"""AI Chat 2.0 Pydantic schemas."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ── Session ──────────────────────────────────────────────────────────────

class SessionCreate(BaseModel):
    title: str = Field(default="New Chat", max_length=200)
    model: str = Field(default="deepseek-chat", max_length=50)
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    source: str = Field(default="web_app", max_length=30)


class SessionUpdate(BaseModel):
    title: str | None = None
    model: str | None = None
    tags: list[str] | None = None
    metadata: dict[str, Any] | None = None
    is_archived: bool | None = None


class SessionRead(BaseModel):
    id: str
    tenant_id: str
    title: str
    model: str
    tags: list[Any] | None = None
    metadata: dict[str, Any] | None = None
    summary: str | None = None
    message_count: int
    total_tokens: int
    source: str
    is_archived: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class SessionSummary(BaseModel):
    """Lightweight session for list views."""
    id: str
    title: str
    summary: str | None = None
    message_count: int
    total_tokens: int
    source: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ── Message ──────────────────────────────────────────────────────────────

class MessageCreate(BaseModel):
    session_id: str
    content: str = Field(..., min_length=1, max_length=10000)
    model: str = Field(default="deepseek-chat", max_length=50)


class MessageRead(BaseModel):
    id: str
    session_id: str
    tenant_id: str
    role: str
    content: str
    tokens_prompt: int
    tokens_completion: int
    latency_ms: int | None = None
    model: str
    memory_context: list[Any] | None = None
    memory_stored: bool
    feedback_score: int | None = None
    feedback_comment: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class MessageFeedback(BaseModel):
    score: int = Field(..., ge=1, le=5)
    comment: str | None = None


# ── Chat (Streaming) ─────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    session_id: str
    content: str = Field(..., min_length=1, max_length=10000)
    model: str = Field(default="deepseek-chat", max_length=50)
    stream: bool = Field(default=True, description="SSE streaming enabled")
    use_memory: bool = Field(default=True, description="Search Graphiti memory")
    store_memory: bool = Field(default=True, description="Store in Graphiti memory")
    template_id: str | None = Field(default=None, description="Prompt template ID")
    template_variables: dict[str, str] = Field(default_factory=dict)


class ChatResponse(BaseModel):
    session_id: str
    message_id: str
    role: str = "assistant"
    content: str
    tokens_prompt: int = 0
    tokens_completion: int = 0
    latency_ms: int | None = None
    model: str


# ── Search ───────────────────────────────────────────────────────────────

class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=500)
    session_id: str | None = None
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


class SearchResult(BaseModel):
    message_id: str
    session_id: str
    session_title: str
    role: str
    content: str
    score: float = 0.0
    created_at: datetime


class SearchResponse(BaseModel):
    results: list[SearchResult]
    total: int
    query: str


# ── Prompt Template ──────────────────────────────────────────────────────

class PromptTemplateCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str | None = None
    role: str = Field(default="system", pattern=r"^(system|user)$")
    content: str = Field(..., min_length=1)
    variables: list[str] = Field(default_factory=list)
    is_default: bool = False


class PromptTemplateUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    role: str | None = None
    content: str | None = None
    variables: list[str] | None = None
    is_default: bool | None = None


class PromptTemplateRead(BaseModel):
    id: str
    tenant_id: str
    name: str
    description: str | None = None
    role: str
    content: str
    variables: list[Any] | None = None
    is_default: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ── Usage ────────────────────────────────────────────────────────────────

class UsageStats(BaseModel):
    total_sessions: int
    total_messages: int
    total_tokens: int
    avg_latency_ms: float = 0.0
    sessions_today: int = 0
    messages_today: int = 0
    tokens_today: int = 0