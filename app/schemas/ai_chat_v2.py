"""AI Chat 2.0 Pydantic schemas."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.config import settings

# The provider/model actually behind this is whatever DEEPSEEK_API_BASE +
# DEEPSEEK_MODEL point at right now -- was a hardcoded "deepseek-chat"
# literal, silently breaking (or worse, silently ignoring model swaps) any
# time that env var moved to a self-hosted model with a different name,
# since nothing on the frontend ever overrides this default.
_default_model = lambda: settings.deepseek_model  # noqa: E731


#  Session 

class SessionCreate(BaseModel):
    title: str = Field(default="New Chat", max_length=200)
    model: str = Field(default_factory=_default_model, max_length=50)
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    source: str = Field(default="web_app", max_length=30)


class SessionUpdate(BaseModel):
    title: str | None = None
    model: str | None = None
    tags: list[str] | None = None
    metadata: dict[str, Any] | None = None
    is_archived: bool | None = None
    is_pinned: bool | None = None


class SessionRead(BaseModel):
    id: str
    tenant_id: str
    title: str
    model: str
    tags: list[Any] | None = None
    # AiChatSession's Python attribute is session_metadata (mapped to DB
    # column "metadata") specifically to avoid colliding with every
    # SQLAlchemy declarative model's own class-level `.metadata` (the table
    # registry) -- but reading `.metadata` off an ORM instance here silently
    # returned that registry object instead of raising AttributeError, so
    # FastAPI's response serialization failed on every single session
    # create/list/get call ("Input should be a valid dictionary" for a real
    # MetaData() instance). validation_alias points this field at the
    # correct attribute while keeping the response JSON key as "metadata".
    metadata: dict[str, Any] | None = Field(default=None, validation_alias="session_metadata")
    summary: str | None = None
    message_count: int
    total_tokens: int
    source: str
    is_archived: bool
    is_pinned: bool = False
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class SessionSummary(BaseModel):
    """Lightweight session for list views."""
    id: str
    title: str
    summary: str | None = None
    message_count: int
    total_tokens: int
    source: str
    is_pinned: bool = False
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


#  Message 

class MessageCreate(BaseModel):
    session_id: str
    content: str = Field(..., min_length=1, max_length=10000)
    model: str = Field(default_factory=_default_model, max_length=50)
    # Used by session branching to copy an existing message verbatim into a
    # new session -- not an AI call, so no credit deduction/generation
    # happens here.
    role: str = Field(default="user", pattern=r"^(user|assistant)$")


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


#  Chat (Streaming) 

class ChatRequest(BaseModel):
    session_id: str
    content: str = Field(..., min_length=1, max_length=10000)
    model: str = Field(default_factory=_default_model, max_length=50)
    stream: bool = Field(default=True, description="SSE streaming enabled")
    use_memory: bool = Field(default=True, description="Search Graphiti memory")
    store_memory: bool = Field(default=True, description="Store in Graphiti memory")
    template_id: str | None = Field(default=None, description="Prompt template ID")
    template_variables: dict[str, str] = Field(default_factory=dict)
    # "Think mode" toggle from the frontend -- the self-hosted reasoning
    # model spends real budget on a separate thinking pass before content,
    # so a bigger ceiling gets used only when the user actually asks for it.
    think_mode: bool = Field(default=False)
    # Context injection: active account/group/broadcast IDs from frontend
    context: dict[str, Any] = Field(default_factory=dict, description="Active context (account_id, group_id, etc.)")


class ChatResponse(BaseModel):
    session_id: str
    message_id: str
    role: str = "assistant"
    content: str
    tokens_prompt: int = 0
    tokens_completion: int = 0
    latency_ms: int | None = None
    model: str
    # "high" | "medium" | "low" | None -- see app.services.ai_chat_service.extract_confidence.
    confidence: str | None = None


#  Search 

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


#  Prompt Template 

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


#  Usage 

class UsageStats(BaseModel):
    total_sessions: int
    total_messages: int
    total_tokens: int
    avg_latency_ms: float = 0.0
    sessions_today: int = 0
    messages_today: int = 0
    tokens_today: int = 0