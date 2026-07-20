"""add broadcasts.distribution_batch_id

Revision ID: a3f9c1d7e6b2
Revises: 006df5b612b3
Create Date: 2026-07-20 03:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a3f9c1d7e6b2"
down_revision: Union[str, None] = "006df5b612b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "broadcasts",
        sa.Column("distribution_batch_id", sa.String(length=36), nullable=True),
    )
    op.create_index(
        "ix_broadcasts_distribution_batch_id",
        "broadcasts",
        ["distribution_batch_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_broadcasts_distribution_batch_id", table_name="broadcasts")
    op.drop_column("broadcasts", "distribution_batch_id")
