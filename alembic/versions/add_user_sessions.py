"""add user_sessions table for persistent API key login"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "add_user_sessions"
down_revision: str | None = "merge_heads_20260713"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "user_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("token_hash", sa.String(64), unique=True, nullable=False, index=True),
        sa.Column("user_id", sa.String(36), nullable=True),
        sa.Column("tenant_id", sa.String(36), nullable=True),
        sa.Column("api_key_id", sa.String(36), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("user_sessions")
