"""Add is_pinned to ai_chat_sessions_v2

The frontend's pinSession() has been calling PUT /sessions/{id} with
{"is_pinned": ...} -- SessionUpdate had no such field (Pydantic silently
dropped it) and the column didn't exist, so pinning never persisted past a
page reload despite the UI showing it as pinned.

Revision ID: add_session_pinned
Revises: add_ai_learning_tables
Create Date: 2026-08-06
"""

import sqlalchemy as sa

from migration_helpers import add_column_if_not_exists

revision = "add_session_pinned"
down_revision = "add_ai_learning_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_not_exists(
        "ai_chat_sessions_v2", sa.Column("is_pinned", sa.Boolean(), nullable=False, server_default=sa.false())
    )


def downgrade() -> None:
    from alembic import op
    op.drop_column("ai_chat_sessions_v2", "is_pinned")
