"""Add AI credit columns to tenants table

The tenants.ai_credits_remaining / ai_credits_reset_tokens / ai_last_refill_at
columns were added to app/models/tenant.py without a matching migration --
the original patch only shipped a raw untracked migrations/*.sql file that
the deploy pipeline (alembic upgrade heads) never runs, so any fresh
container start 500s on the admin dashboard with
"column tenants.ai_credits_remaining does not exist".

Revision ID: 006_add_ai_credits
Revises: 005_fix_performance_indexes
Create Date: 2026-07-31 09:00:00.000000
"""
from typing import Sequence, Union

from alembic import op

revision: str = "006_add_ai_credits"
down_revision: Union[str, None] = "005_fix_performance_indexes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS ai_credits_remaining INTEGER NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS ai_credits_reset_tokens INTEGER NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS ai_last_refill_at TIMESTAMP")


def downgrade() -> None:
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS ai_last_refill_at")
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS ai_credits_reset_tokens")
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS ai_credits_remaining")
