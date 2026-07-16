"""add broadcast schedule tables and validation columns

Revision ID: add_broadcast_schedule
Revises: merge_session_and_inline_buttons
Create Date: 2026-07-17 04:06:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "add_broadcast_schedule"
down_revision: Union[str, Sequence[str], None] = "merge_session_and_inline_buttons"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE broadcasts ADD COLUMN IF NOT EXISTS validation_status VARCHAR(20) NULL")
    op.execute("ALTER TABLE broadcasts ADD COLUMN IF NOT EXISTS validation_error TEXT NULL")

    op.create_table(
        "broadcast_schedule_entries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), index=True, nullable=False),
        sa.Column("broadcast_id", sa.String(36), nullable=True),
        sa.Column("campaign_id", sa.String(36), nullable=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("scheduled_at", sa.DateTime, index=True, nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["broadcast_id"], ["broadcasts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="SET NULL"),
    )


def downgrade() -> None:
    op.drop_table("broadcast_schedule_entries")
    op.execute("ALTER TABLE broadcasts DROP COLUMN IF EXISTS validation_error")
    op.execute("ALTER TABLE broadcasts DROP COLUMN IF EXISTS validation_status")
