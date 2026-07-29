"""Create Knowledge Base tables (pgvector, documents, chunks, search logs, feedback)

Revision ID: 003_create_kb_tables
Revises: f7a8b9c0d1e2
Create Date: 2026-07-30 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "003_create_kb_tables"
down_revision: Union[str, None] = "f7a8b9c0d1e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Enable pgvector extension
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # ── Documents ──
    op.create_table(
        "kb_documents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False, index=True),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("source_url", sa.String(1000), nullable=True),
        sa.Column("source_type", sa.String(50), server_default="manual"),
        sa.Column("collection", sa.String(100), nullable=False, index=True),
        sa.Column("permission_groups", postgresql.ARRAY(sa.String), server_default="{}"),
        sa.Column("extra", postgresql.JSONB, server_default="{}"),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("is_published", sa.Boolean, server_default="true"),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, server_default=sa.func.now()),
    )

    # ── Chunks ──
    op.create_table(
        "kb_chunks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("document_id", sa.String(36), sa.ForeignKey("kb_documents.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("chunk_index", sa.Integer, server_default="0"),
        sa.Column("chunk_type", sa.String(50), server_default="paragraph"),
        sa.Column("token_count", sa.Integer, nullable=True),
        sa.Column("extra", postgresql.JSONB, server_default="{}"),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )

    # Add vector embedding column (pgvector)
    op.execute("ALTER TABLE kb_chunks ADD COLUMN embedding vector(1536)")

    # ── Search Logs ──
    op.create_table(
        "kb_search_logs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False, index=True),
        sa.Column("query", sa.Text, nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("results", postgresql.ARRAY(sa.String), nullable=True),
        sa.Column("latency_ms", sa.Integer, nullable=True),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )

    # ── Feedback ──
    op.create_table(
        "kb_feedback",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("search_log_id", sa.String(36), sa.ForeignKey("kb_search_logs.id"), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("rating", sa.Integer, nullable=False),
        sa.Column("comment", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("kb_feedback")
    op.drop_table("kb_search_logs")
    op.drop_table("kb_chunks")
    op.drop_table("kb_documents")
