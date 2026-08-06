"""Create ai_memories table (Auto Memory Engine)

Revision ID: create_ai_memories
Revises: 3dbfcaede101
Create Date: 2026-08-07
"""

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

from migration_helpers import create_table_if_not_exists, create_index_if_not_exists

revision = "create_ai_memories"
down_revision = "3dbfcaede101"
branch_labels = None
depends_on = None


def upgrade() -> None:
    create_table_if_not_exists(
        "ai_memories",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("owner_type", sa.String(length=20), nullable=False),
        sa.Column("owner_key", sa.String(length=64), nullable=False),
        sa.Column("category", sa.String(length=50), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("memory_score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("embedding", Vector(1536), nullable=True),
        sa.Column("source_question", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()")),
    )
    create_index_if_not_exists("ix_ai_memories_owner", "ai_memories", ["owner_type", "owner_key"])
    create_index_if_not_exists("ix_ai_memories_category", "ai_memories", ["category"])


def downgrade() -> None:
    op.drop_table("ai_memories")
