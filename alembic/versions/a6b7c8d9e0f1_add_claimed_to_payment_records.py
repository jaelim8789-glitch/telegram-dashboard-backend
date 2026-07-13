"""Add claimed column to payment_records for one-time key delivery.

Revision ID: a6b7c8d9e0f1
Revises: a5b6c7d8e9f0
Create Date: 2026-07-13 10:25:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a6b7c8d9e0f1"
down_revision: Union[str, None] = "a5b6c7d8e9f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "payment_records",
        sa.Column("claimed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("payment_records", "claimed")
