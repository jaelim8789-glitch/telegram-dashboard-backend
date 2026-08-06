"""Create Knowledge Base tables (kb_documents, kb_chunks, kb_search_logs, kb_feedback)

These tables were created in production outside alembic (via app startup /
metadata.create_all) and never had a migration -- so a fresh CI database ran
`alembic upgrade head` into add_ai_learning_tables which tried to inspect
kb_documents (which didn't exist) and blew up with NoSuchTableError.

This migration creates the tables so a fresh DB can run the full chain.
On production the tables already exist, so every create is skipped
(create_table_if_not_exists).

Revision ID: create_kb_tables
Revises: add_token_valid_after
Create Date: 2026-08-06
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from pgvector.sqlalchemy import Vector

from migration_helpers import create_table_if_not_exists, create_index_if_not_exists

revision = "create_kb_tables"
down_revision = "add_token_valid_after"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # kb_documents
    create_table_if_not_exists(
        "kb_documents",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source_url", sa.String(length=1000), nullable=True),
        sa.Column("source_type", sa.String(length=50), nullable=False, server_default="manual"),
        sa.Column("collection", sa.String(length=100), nullable=False),
        sa.Column("permission_groups", postgresql.ARRAY(sa.String()), nullable=True),
        sa.Column("extra", postgresql.JSON(), nullable=True),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.Column("is_published", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("parent_id", sa.String(length=36), nullable=True),
        sa.Column("is_latest", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()")),
    )

    # kb_chunks (pgvector embedding)
    create_table_if_not_exists(
        "kb_chunks",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("chunk_type", sa.String(length=50), nullable=False, server_default="paragraph"),
        sa.Column("token_count", sa.Integer(), nullable=True),
        sa.Column("embedding", Vector(1536), nullable=True),
        sa.Column("extra", postgresql.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()")),
    )

    # kb_search_logs
    create_table_if_not_exists(
        "kb_search_logs",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=True),
        sa.Column("results", postgresql.JSON(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()")),
    )

    # kb_feedback
    create_table_if_not_exists(
        "kb_feedback",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("search_log_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=True),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()")),
    )

    # Indexes
    create_index_if_not_exists("ix_kb_documents_collection", "kb_documents", ["collection"])
    create_index_if_not_exists("ix_kb_chunks_document_id", "kb_chunks", ["document_id"])

    # FKs
    bind = op.get_bind()
    if bind.dialect.has_table(bind, "kb_chunks"):
        insp = sa.inspect(bind)
        existing_fks = [fk["name"] for fk in insp.get_foreign_keys("kb_chunks")]
        if "fk_kb_chunks_document_id" not in existing_fks:
            op.create_foreign_key("fk_kb_chunks_document_id", "kb_chunks", "kb_documents", ["document_id"], ["id"])
    if bind.dialect.has_table(bind, "kb_feedback"):
        insp = sa.inspect(bind)
        existing_fks = [fk["name"] for fk in insp.get_foreign_keys("kb_feedback")]
        if "fk_kb_feedback_search_log_id" not in existing_fks:
            op.create_foreign_key("fk_kb_feedback_search_log_id", "kb_feedback", "kb_search_logs", ["search_log_id"], ["id"])


def downgrade() -> None:
    op.drop_table("kb_feedback")
    op.drop_table("kb_search_logs")
    op.drop_table("kb_chunks")
    op.drop_table("kb_documents")
