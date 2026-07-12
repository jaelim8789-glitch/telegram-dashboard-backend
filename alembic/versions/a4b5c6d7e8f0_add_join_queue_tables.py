"""Add join_queue_items and join_queue_configs tables.

Revision ID: a4b5c6d7e8f0
Revises: b4d2f6a8c1e3
Create Date: 2026-07-13 00:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers, used by Alembic.
revision: str = "a4b5c6d7e8f0"
down_revision: Union[str, None] = "b4d2f6a8c1e3"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    # ── join_queue_items ────────────────────────────────────────────────
    op.create_table(
        "join_queue_items",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("account_id", sa.String(), sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("raw_link", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("chat_type", sa.String(), nullable=True),
        sa.Column("username", sa.String(), nullable=True),
        sa.Column("chat_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="queued", index=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("flood_wait_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("delay_before_seconds", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
    )

    # ── join_queue_configs ──────────────────────────────────────────────
    op.create_table(
        "join_queue_configs",
        sa.Column("account_id", sa.String(), sa.ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("is_paused", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("joins_per_hour", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("max_daily_joins", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("join_queue_configs")
    op.drop_table("join_queue_items")