"""add trial_expires_at to tenants

Revision ID: d5a1c2b3e4f0
Revises: f8a5d3b2c1e0
Create Date: 2026-07-11 14:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d5a1c2b3e4f0"
down_revision: Union[str, None] = "c7f5e9d1b2a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column("trial_expires_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("tenants", "trial_expires_at")
