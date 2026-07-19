"""AI Chat 2.0 models: sessions, messages, prompt templates."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AiChatSession(Base):
    """AI Chat 2.0 session.

    Each session groups messages under a tenant-scoped conversation.
    Supports metadata tagging for search/filter and auto-summary.
    """

    __tablename__ = "ai_chat_sessions_v2"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200), default="New Chat")
    model: Mapped[str] = mapped_column(String(50), default="deepseek-chat")

    # Session metadata
    tags: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)
    session_metadata: Mapped[dict | None] = mapped_column("metadata", JSON, nullable=True, default=dict)

    # Auto-generated summary for fast recall
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Message stats
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)

    # Reference to source (e.g. 'web_app', 'telegram_bot', 'api')
    source: Mapped[str] = mapped_column(String(30), default="web_app")

    # Timing
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    # Soft delete
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False)


class AiChatMessageV2(Base):
    """AI Chat 2.0 message within a session.

    Stores both user and assistant messages with token usage and latency.
    Supports full-text search via pg_trgm or LIKE on content.
    """

    __tablename__ = "ai_chat_messages_v2"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("ai_chat_sessions_v2.id", ondelete="CASCADE"), index=True,
    )
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id", ondelete="CASCADE"), index=True)

    role: Mapped[str] = mapped_column(String(20), nullable=False)  # user / assistant / system
    content: Mapped[str] = mapped_column(Text, nullable=False)

    # Streaming metadata
    tokens_prompt: Mapped[int] = mapped_column(Integer, default=0)
    tokens_completion: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    model: Mapped[str] = mapped_column(String(50), default="deepseek-chat")

    # Memory integration
    memory_context: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)
    memory_stored: Mapped[bool] = mapped_column(Boolean, default=False)

    # Feedback
    feedback_score: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 1-5
    feedback_comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class AiChatPromptTemplate(Base):
    """AI Chat 2.0 prompt template.

    Reusable system/user prompt templates with variables. Tenant-scoped.
    """

    __tablename__ = "ai_chat_prompt_templates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), index=True,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)

    role: Mapped[str] = mapped_column(String(20), default="system")  # system / user
    content: Mapped[str] = mapped_column(Text, nullable=False)

    # Template variables: {{variable_name}}
    variables: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())