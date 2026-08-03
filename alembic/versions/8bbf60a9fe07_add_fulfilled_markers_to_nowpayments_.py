"""add fulfilled markers to nowpayments_transactions

Revision ID: 8bbf60a9fe07
Revises: fix_tenant_ai_credits
Create Date: 2026-08-03 21:07:25.903321

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from migration_helpers import add_column_if_not_exists, drop_column_if_exists


# revision identifiers, used by Alembic.
revision: str = '8bbf60a9fe07'
down_revision: Union[str, None] = 'fix_tenant_ai_credits'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Idempotent fulfillment marker — prevents a concurrent duplicate webhook
    # from activating a plan / issuing an API key twice.
    add_column_if_not_exists(
        "nowpayments_transactions",
        sa.Column("fulfilled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    add_column_if_not_exists(
        "nowpayments_transactions",
        sa.Column("fulfilled_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    drop_column_if_exists("nowpayments_transactions", "fulfilled_at")
    drop_column_if_exists("nowpayments_transactions", "fulfilled")
