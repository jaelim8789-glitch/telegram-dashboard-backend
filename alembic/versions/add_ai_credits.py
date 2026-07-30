"""Add AI credit columns to tenants table

Revision ID: 006_add_ai_credits
Revises: 005_fix_performance_indexes
Create Date: 2026-07-31 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "006_add_ai_credits"
down_revision: Union[str, None] = "005_fix_performance_indexes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column("ai_credits_remaining", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("tenants", sa.Column("ai_credits_reset_tokens", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("tenants", sa.Column("ai_last_refill_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("tenants", "ai_last_refill_at")
    op.drop_column("tenants", "ai_credits_reset_tokens")
    op.drop_column("tenants", "ai_credits_remaining")
