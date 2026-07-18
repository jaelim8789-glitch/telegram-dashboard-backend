"""AI Reply 2.0 Pydantic schemas."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ── Persona ──────────────────────────────────────────────────────────────

class PersonaStyle(BaseModel):
    formality: float = Field(default=0.5, ge=0.0, le=1.0)
    emoji_usage: str = Field(default="minimal", pattern=r"^(none|minimal|moderate|frequent)$")
    greeting_style: str = Field(default="simple", pattern=r"^(none|simple|warm|formal)$")
    signature: str | None = None
    max_length: int = Field(default=500, ge=50, le=2000)
    language: str = Field(default="ko", min_length=2, max_length=5)
    custom_instructions: str | None = None


class BusinessInfo(BaseModel):
    company_name: str | None = None
    industry: str | None = None
    offerings: list[str] = Field(default_factory=list)
    brand_voice: str | None = None
    keywords: list[str] = Field(default_factory=list)
    avoid_topics: list[str] = Field(default_factory=list)


class PersonaCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    tone: str = Field(default="professional")
    style: PersonaStyle = Field(default_factory=PersonaStyle)
    business_info: BusinessInfo = Field(default_factory=BusinessInfo)


class PersonaUpdate(BaseModel):
    name: str | None = None
    tone: str | None = None
    style: PersonaStyle | None = None
    business_info: BusinessInfo | None = None
    is_active: bool | None = None


class PersonaRead(BaseModel):
    id: str
    tenant_id: str
    account_id: str
    name: str
    is_active: bool
    tone: str
    style: dict[str, Any]
    business_info: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ── Conversation ─────────────────────────────────────────────────────────

class ConversationMessage(BaseModel):
    role: str  # "user" | "assistant"
    content: str
    timestamp: str | None = None
    message_id: int | None = None


class ConversationRead(BaseModel):
    id: str
    tenant_id: str
    account_id: str
    chat_id: str
    chat_title: str | None = None
    messages: list[dict[str, Any]]
    summary: str | None = None
    message_count: int
    last_message_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ── Suggestion ───────────────────────────────────────────────────────────

class SuggestionOption(BaseModel):
    text: str
    confidence: float = Field(..., ge=0.0, le=1.0)
    reason: str | None = None


class SuggestionContext(BaseModel):
    persona_id: str | None = None
    persona_name: str | None = None
    tone: str = "professional"
    conversation_summary: str | None = None
    memory_context: list[str] = Field(default_factory=list)
    recent_messages: int = 0


class SuggestionGenerateRequest(BaseModel):
    account_id: str
    chat_id: str
    chat_title: str | None = None
    user_id: str
    user_name: str | None = None
    incoming_message: str = Field(..., min_length=1, max_length=4000)
    persona_id: str | None = None
    auto_reply_enabled: bool = False


class SuggestionGenerateResponse(BaseModel):
    id: str
    suggestions: dict[str, Any]
    context: dict[str, Any]
    status: str
    response_time_ms: int | None = None


class SuggestionRead(BaseModel):
    id: str
    tenant_id: str
    account_id: str
    chat_id: str
    chat_title: str | None = None
    user_id: str
    user_name: str | None = None
    incoming_message: str
    suggestions: dict[str, Any]
    context: dict[str, Any]
    status: str
    auto_reply_enabled: bool
    auto_reply_sent: bool
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    selected_suggestion: str | None = None
    custom_reply: str | None = None
    feedback: dict[str, Any] | None = None
    response_time_ms: int | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class SuggestionReviewRequest(BaseModel):
    status: str = Field(..., pattern=r"^(approved|dismissed)$")
    selected_suggestion: str | None = Field(
        None, pattern=r"^(primary|alternative_\d|custom)$"
    )
    custom_reply: str | None = None


class SuggestionFeedbackRequest(BaseModel):
    rating: int = Field(..., ge=1, le=5)
    comment: str | None = None
    was_helpful: bool = True


# ── Auto-Reply Workflow ──────────────────────────────────────────────────

class AutoReplyV2Settings(BaseModel):
    account_id: str
    auto_reply_enabled: bool
    ai_fallback_enabled: bool
    min_confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    max_replies_per_day: int = Field(default=50, ge=1, le=500)
    active_persona_id: str | None = None


class AutoReplyV2SettingsUpdate(BaseModel):
    auto_reply_enabled: bool | None = None
    ai_fallback_enabled: bool | None = None
    min_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    max_replies_per_day: int | None = Field(default=None, ge=1, le=500)
    active_persona_id: str | None = None