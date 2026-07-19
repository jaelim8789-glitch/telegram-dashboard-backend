import uuid
from datetime import datetime

from sqlalchemy import String, Text, DateTime, ForeignKey, Integer, Boolean, func, JSON
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# JSONB on PostgreSQL; fall back to JSON on SQLite so the test suite's SQLite
# fallback can create these tables (JSONB has no SQLite compiler).
JSONType = JSONB().with_variant(JSON(), "sqlite")


class AiAgent(Base):
    """AI Agent — 사용자가 생성하는 AI 직원."""
    __tablename__ = "ai_agents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(String(50))
    system_prompt: Mapped[str] = mapped_column(Text, default="")
    tools: Mapped[list] = mapped_column(JSONType, default=list)
    is_template: Mapped[bool] = mapped_column(Boolean, default=False)
    template_price: Mapped[int] = mapped_column(Integer, default=0)
    template_purchases: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    total_messages: Mapped[int] = mapped_column(Integer, default=0)
    level: Mapped[int] = mapped_column(Integer, default=1)
    exp: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class AiChat(Base):
    """Agent별 채팅방."""
    __tablename__ = "ai_chats"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    agent_id: Mapped[str] = mapped_column(ForeignKey("ai_agents.id", ondelete="CASCADE"), index=True)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class AiMessage(Base):
    """채팅방 내 메시지."""
    __tablename__ = "ai_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    chat_id: Mapped[str] = mapped_column(ForeignKey("ai_chats.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(20))  # user / agent / tool
    content: Mapped[str] = mapped_column(Text, default="")
    tool_name: Mapped[str | None] = mapped_column(String(50), nullable=True)
    tool_button_label: Mapped[str | None] = mapped_column(String(100), nullable=True)
    tool_payload: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    tokens_used: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())