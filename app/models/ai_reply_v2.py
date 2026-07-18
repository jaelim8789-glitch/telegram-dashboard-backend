"""AI Reply 2.0 models: personas, conversation context, enhanced suggestions."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class AiReplyPersona(Base):
    """Persona/tone configuration for AI Reply 2.0.

    Each account can have multiple personas. The active one is used for
    generating reply suggestions. Personas are tenant-scoped.
    """

    __tablename__ = "ai_reply_personas"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[str] = mapped_column(String(36), ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)

    # Tone configuration
    tone: Mapped[str] = mapped_column(String(20), default="professional")
    # professional, casual, friendly, formal, witty, empathetic, concise, enthusiastic

    # Style configuration
    style: Mapped[dict] = mapped_column(JSON, default=dict)
    # {
    #   "formality": 0.0-1.0,
    #   "emoji_usage": "none" | "minimal" | "moderate" | "frequent",
    #   "greeting_style": "none" | "simple" | "warm" | "formal",
    #   "signature": str | null,
    #   "max_length": int (default 500),
    #   "language": str (default "ko"),
    #   "custom_instructions": str | null
    # }

    # Business context
    business_info: Mapped[dict] = mapped_column(JSON, default=dict)
    # {
    #   "company_name": str | null,
    #   "industry": str | null,
    #   "offerings": [str],
    #   "brand_voice": str | null,
    #   "keywords": [str],
    #   "avoid_topics": [str]
    # }

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class AiReplyConversation(Base):
    """Conversation context tracking for AI Reply 2.0.

    Stores recent message history per (account_id, chat_id) to provide
    context-aware suggestions. Automatically pruned to last N messages.
    """

    __tablename__ = "ai_reply_conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[str] = mapped_column(String(36), ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    chat_id: Mapped[str] = mapped_column(String(100), index=True)
    chat_title: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # Message history as JSON array, newest first
    # [{"role": "user"|"assistant", "content": str, "timestamp": "ISO8601", "message_id": int}, ...]
    messages: Mapped[list] = mapped_column(JSON, default=list)

    # Summary of the conversation for long-term context
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Metadata
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        # Unique constraint: one conversation record per (account_id, chat_id)
        # Handled at the application level with upsert logic
    )


class AiReplySuggestionV2(Base):
    """Enhanced AI Reply 2.0 suggestion with multiple options, confidence, and workflow state."""

    __tablename__ = "ai_reply_suggestions_v2"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[str] = mapped_column(String(36), ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    chat_id: Mapped[str] = mapped_column(String(100), index=True)
    chat_title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    user_id: Mapped[str] = mapped_column(String(100))
    user_name: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Incoming message that triggered the suggestion
    incoming_message: Mapped[str] = mapped_column(Text)

    # Multiple suggestion options with confidence scores
    suggestions: Mapped[dict] = mapped_column(JSON, default=dict)
    # {
    #   "primary": {"text": "...", "confidence": 0.95, "reason": "..."},
    #   "alternatives": [
    #     {"text": "...", "confidence": 0.7, "reason": "..."},
    #     {"text": "...", "confidence": 0.5, "reason": "..."}
    #   ]
    # }

    # Context used for generation
    context: Mapped[dict] = mapped_column(JSON, default=dict)
    # {
    #   "persona_id": str | null,
    #   "persona_name": str | null,
    #   "tone": str,
    #   "conversation_summary": str | null,
    #   "memory_context": [str],
    #   "recent_messages": int
    # }

    # Workflow state
    status: Mapped[str] = mapped_column(String(20), default="pending")
    # pending, reviewed, approved, sent, dismissed

    # Auto-reply workflow
    auto_reply_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_reply_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_reply_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Review tracking
    reviewed_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    selected_suggestion: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # "primary" | "alternative_0" | "alternative_1" | "custom"

    # Custom reply if user edited the suggestion
    custom_reply: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Feedback
    feedback: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # {"rating": 1-5, "comment": str | null, "was_helpful": bool}

    # Timing
    response_time_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())