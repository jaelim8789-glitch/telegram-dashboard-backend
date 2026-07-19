"""add trial_expiry_notified to tenants

Revision ID: 51fdc53fc518
Revises: 07caecb49b88
Create Date: 2026-07-19 21:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "51fdc53fc518"
down_revision: Union[str, None] = "07caecb49b88"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tenants", sa.Column("trial_expiry_notified", sa.Boolean(), server_default=sa.false(), nullable=False)
    )


def downgrade() -> None:
    op.drop_column("tenants", "trial_expiry_notified")
