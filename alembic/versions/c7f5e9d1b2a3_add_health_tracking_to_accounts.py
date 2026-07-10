"""Add health tracking columns to accounts table.

Revision ID: c7f5e9d1b2a3
Revises: a3b5c7d8e9f0
Create Date: 2026-07-11 01:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c7f5e9d1b2a3"
down_revision: Union[str, None] = "a3b5c7d8e9f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("accounts", sa.Column("last_error", sa.Text(), nullable=True))
    op.add_column("accounts", sa.Column("last_error_at", sa.DateTime(), nullable=True))
    op.add_column("accounts", sa.Column("last_success_at", sa.DateTime(), nullable=True))
    op.add_column("accounts", sa.Column("health_checked_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("accounts", "health_checked_at")
    op.drop_column("accounts", "last_success_at")
    op.drop_column("accounts", "last_error_at")
    op.drop_column("accounts", "last_error")
