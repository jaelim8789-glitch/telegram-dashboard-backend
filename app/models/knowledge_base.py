import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import ARRAY, Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Document(Base):
    __tablename__ = "kb_documents"

    # id (and every kb_documents/kb_chunks FK pointing at it) was typed as a
    # real postgres UUID column, but the actual column is
    # character varying(36) -- any WHERE/comparison on it (e.g. db.refresh())
    # renders an "::UUID" cast that postgres refuses to compare against
    # varchar ("operator does not exist: character varying = uuid").
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    source_type: Mapped[str] = mapped_column(String(50), default="manual")
    collection: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    # Pre-existing model/DB mismatch: this was declared as JSON but the
    # actual kb_documents.permission_groups column is a native postgres
    # character varying[] array -- every insert (via ingest_document())
    # failed with DatatypeMismatchError until this was corrected to match.
    permission_groups: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    extra: Mapped[dict | None] = mapped_column("extra", JSON, default=dict)
    created_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    is_published: Mapped[bool] = mapped_column(Boolean, default=True)
    # Knowledge Versioning
    version: Mapped[int] = mapped_column(Integer, default=1)
    parent_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("kb_documents.id"), nullable=True)
    is_latest: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("now()"), onupdate=text("now()"))

    chunks: Mapped[list["Chunk"]] = relationship(back_populates="document", cascade="all, delete-orphan")


class Chunk(Base):
    __tablename__ = "kb_chunks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    document_id: Mapped[str] = mapped_column(String(36), ForeignKey("kb_documents.id", ondelete="CASCADE"), nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, default=0)
    chunk_type: Mapped[str] = mapped_column(String(50), default="paragraph")
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Same pre-existing model/DB mismatch as Document.permission_groups --
    # kb_chunks.embedding is a real pgvector `vector(1536)` column (used by
    # the raw-SQL cosine search in search_knowledge_base()), not JSON.
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1536), nullable=True)
    extra: Mapped[dict | None] = mapped_column("extra", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("now()"))

    document: Mapped["Document"] = relationship(back_populates="chunks")


class SearchLog(Base):
    __tablename__ = "kb_search_logs"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    query: Mapped[str] = mapped_column(Text, nullable=False)
    user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    results: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
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


class KnowledgeCandidate(Base):
    __tablename__ = "kb_candidates"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    feedback_score: Mapped[float] = mapped_column(Float, default=0.0)
    feedback_count: Mapped[int] = mapped_column(Integer, default=0)
    model_name: Mapped[str] = mapped_column(String(100), default="unknown")
    tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    response_time_ms: Mapped[int] = mapped_column(Integer, default=0)
    ai_version: Mapped[str] = mapped_column(String(100), default="")
    prompt_version: Mapped[str] = mapped_column(String(50), default="v1")
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)  # pending / approved / rejected
    approval_reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    approval_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("now()"))
