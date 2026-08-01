import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Document(Base):
    __tablename__ = "kb_documents"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    source_type: Mapped[str] = mapped_column(String(50), default="manual")
    collection: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    permission_groups: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    extra: Mapped[dict | None] = mapped_column("extra", JSONB, default=dict)
    created_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    is_published: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("now()"), onupdate=text("now()"))

    chunks: Mapped[list["Chunk"]] = relationship(back_populates="document", cascade="all, delete-orphan")


class Chunk(Base):
    __tablename__ = "kb_chunks"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    document_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("kb_documents.id", ondelete="CASCADE"), nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, default=0)
    chunk_type: Mapped[str] = mapped_column(String(50), default="paragraph")
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    embedding: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    extra: Mapped[dict | None] = mapped_column("extra", JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("now()"))

    document: Mapped["Document"] = relationship(back_populates="chunks")


class SearchLog(Base):
    __tablename__ = "kb_search_logs"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    query: Mapped[str] = mapped_column(Text, nullable=False)
    user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    results: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("now()"))


class Feedback(Base):
    __tablename__ = "kb_feedback"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    search_log_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("kb_search_logs.id"), nullable=False)
    user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("now()"))
