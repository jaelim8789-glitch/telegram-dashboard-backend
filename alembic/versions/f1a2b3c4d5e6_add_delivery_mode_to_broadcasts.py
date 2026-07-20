"""add broadcasts.delivery_mode

Revision ID: f1a2b3c4d5e6
Revises: 2ccdcc70c303
Create Date: 2026-07-20 16:36:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, None] = "2ccdcc70c303"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "broadcasts",
        sa.Column("delivery_mode", sa.String(length=20), nullable=False, server_default="normal"),
    )


def downgrade() -> None:
    op.drop_column("broadcasts", "delivery_mode")
