"""add missing api_keys.purpose and broadcasts.batch_size columns

These columns exist on the SQLAlchemy models (app/models/api_key.py,
app/models/broadcast.py) but were never given a migration, so
production was missing them entirely — breaking admin API key
issuance (UndefinedColumnError: purpose) and the recurring-broadcast
dispatch job every 30s (UndefinedColumnError: batch_size).

Revision ID: b3f8e2a1c4d5
Revises: 6ff03da70bf4
Create Date: 2026-07-21 16:45:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b3f8e2a1c4d5'
down_revision: Union[str, None] = '6ff03da70bf4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "api_keys",
        sa.Column("purpose", sa.String(length=20), nullable=False, server_default="payment_issued"),
    )
    op.add_column(
        "broadcasts",
        sa.Column("batch_size", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("broadcasts", "batch_size")
    op.drop_column("api_keys", "purpose")
