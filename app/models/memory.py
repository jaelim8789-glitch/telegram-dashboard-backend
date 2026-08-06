import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, Float, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class MemoryEntry(Base):
    """Auto Memory Engine — selective long-term memory.

    Stores only high-value facts (memory_score >= 90) per owner, deduplicated
    against existing entries via embedding similarity. Supports both members
    (tenant_id) and guests (owner_key = IP) so every user gets memory.
    """

    __tablename__ = "ai_memories"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # "tenant" | "guest"
    owner_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    # tenant_id for members, client IP for guests
    owner_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # User Profile | Preferences | Projects | Company | Goals | Long-term Facts | Conversation Insights
    category: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    memory_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1536), nullable=True)
    source_question: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("now()"), onupdate=text("now()"))
