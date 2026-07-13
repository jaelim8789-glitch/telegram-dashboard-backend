"""add broadcast_mode column to broadcasts

Revision ID: 8a1b2c3d4e5f
Revises: 2f1d3c4b5a6e
Create Date: 2026-07-13 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "8a1b2c3d4e5f"
down_revision: Union[str, None] = "2f1d3c4b5a6e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "broadcasts",
        sa.Column("broadcast_mode", sa.String(20), server_default="standard", nullable=False),
    )
    op.execute("UPDATE broadcasts SET broadcast_mode = 'standard' WHERE broadcast_mode IS NULL")


def downgrade() -> None:
    op.drop_column("broadcasts", "broadcast_mode")
