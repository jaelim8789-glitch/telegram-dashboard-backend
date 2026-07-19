"""add referral_rewarded to tenants

Revision ID: 07caecb49b88
Revises: 5145c80244a3
Create Date: 2026-07-19 21:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "07caecb49b88"
down_revision: Union[str, None] = "5145c80244a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column("referral_rewarded", sa.Boolean(), server_default=sa.false(), nullable=False))


def downgrade() -> None:
    op.drop_column("tenants", "referral_rewarded")
