"""add checkin_streak/last_checkin_at to tenants

Revision ID: 5145c80244a3
Revises: 2b68d1568159
Create Date: 2026-07-19 21:15:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from migration_helpers import add_column_if_not_exists


revision: str = "5145c80244a3"
down_revision: Union[str, None] = "2b68d1568159"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    add_column_if_not_exists("tenants", sa.Column("checkin_streak", sa.Integer(), server_default="0", nullable=False))
    add_column_if_not_exists("tenants", sa.Column("last_checkin_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("tenants", "last_checkin_at")
    op.drop_column("tenants", "checkin_streak")
