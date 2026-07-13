"""Add reply_to_msg_id column to broadcasts for reply delivery mode.

Revision ID: d8e9f0a1b2c3
Revises: merge_heads_20260713
Create Date: 2026-07-13 16:34:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d8e9f0a1b2c3"
down_revision: Union[str, None] = "merge_heads_20260713"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "broadcasts",
        sa.Column("reply_to_msg_id", sa.Integer(), nullable=True, default=None),
    )


def downgrade() -> None:
    op.drop_column("broadcasts", "reply_to_msg_id")
