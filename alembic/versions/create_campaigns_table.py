"""create campaigns table

Revision ID: create_campaigns_table
Revises: merge_session_and_inline_buttons
Create Date: 2026-07-19 00:00:00.000000

The Campaign model (app/models/campaign.py) was added without a
corresponding migration. add_broadcast_schedule.py already creates a
foreign key to campaigns.id, which crashed deploys with
UndefinedTableError since the table never existed. This inserts the
missing create_table ahead of add_broadcast_schedule in the chain.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "create_campaigns_table"
down_revision: Union[str, Sequence[str], None] = "merge_session_and_inline_buttons"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "campaigns",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("goal", sa.String(50), nullable=True),
        sa.Column("total_broadcasts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("total_sent", sa.Integer, nullable=False, server_default="0"),
        sa.Column("total_failed", sa.Integer, nullable=False, server_default="0"),
        sa.Column("total_recipients", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", sa.DateTime, nullable=True),
        sa.Column("completed_at", sa.DateTime, nullable=True),
    )
    # Columns are NOT declared index=True above (that would make create_table
    # auto-generate these same indexes and collide with the explicit calls
    # below - verified against DuplicateTableError while testing this
    # migration).
    op.create_index("ix_campaigns_tenant_id", "campaigns", ["tenant_id"])
    op.create_index("ix_campaigns_status", "campaigns", ["status"])


def downgrade() -> None:
    op.drop_index("ix_campaigns_status", table_name="campaigns")
    op.drop_index("ix_campaigns_tenant_id", table_name="campaigns")
    op.drop_table("campaigns")
