"""add campaign_id, group_ids, groups_resolved to broadcasts

Revision ID: add_campaign_fields_broadcasts
Revises: 8fd32dca8ec4
Create Date: 2026-07-19 00:10:00.000000

app/models/broadcast.py declares campaign_id, group_ids, and
groups_resolved, but no prior migration ever added them - broadcasts
queries were crashing production with UndefinedColumnError on
campaign_id (same root cause pattern as the missing campaigns table:
model fields added without a matching migration).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "add_campaign_fields_broadcasts"
down_revision: Union[str, Sequence[str], None] = "8fd32dca8ec4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "broadcasts",
        sa.Column("campaign_id", sa.String(36), sa.ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True),
    )
    op.create_index("ix_broadcasts_campaign_id", "broadcasts", ["campaign_id"])
    op.add_column("broadcasts", sa.Column("group_ids", sa.JSON(), nullable=True))
    op.add_column(
        "broadcasts",
        sa.Column("groups_resolved", sa.Boolean(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("broadcasts", "groups_resolved")
    op.drop_column("broadcasts", "group_ids")
    op.drop_index("ix_broadcasts_campaign_id", table_name="broadcasts")
    op.drop_column("broadcasts", "campaign_id")
