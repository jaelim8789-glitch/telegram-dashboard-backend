"""add missing tenant columns for AI credits and wallet

Revision ID: fix_tenant_ai_credits
Revises: escrow_trust_001
Create Date: 2026-08-03
"""

from alembic import op
import sqlalchemy as sa
from migration_helpers import add_column_if_not_exists

revision = "fix_tenant_ai_credits"
down_revision = "escrow_trust_001"
branch_labels = None
depends_on = None


def _column_exists(table: str, column: str) -> bool:
    bind = op.get_bind()
    result = bind.execute(sa.text(
        f"SELECT EXISTS (SELECT 1 FROM information_schema.columns "
        f"WHERE table_name = '{table}' AND column_name = '{column}')"
    ))
    return result.scalar()


def upgrade() -> None:
    # AI credits system
    if not _column_exists("tenants", "ai_credits_remaining"):
        add_column_if_not_exists("tenants", sa.Column("ai_credits_remaining", sa.Integer, server_default="0"))
    if not _column_exists("tenants", "ai_credits_reset_tokens"):
        add_column_if_not_exists("tenants", sa.Column("ai_credits_reset_tokens", sa.Integer, server_default="0"))
    if not _column_exists("tenants", "ai_last_refill_at"):
        add_column_if_not_exists("tenants", sa.Column("ai_last_refill_at", sa.DateTime(timezone=True), nullable=True))

    # Wallet
    if not _column_exists("tenants", "wallet_address"):
        add_column_if_not_exists("tenants", sa.Column("wallet_address", sa.String(100), nullable=True))

    # Distributor
    if not _column_exists("tenants", "is_distributor"):
        add_column_if_not_exists("tenants", sa.Column("is_distributor", sa.Boolean, server_default="false"))

    # Check-in
    if not _column_exists("tenants", "checkin_streak"):
        add_column_if_not_exists("tenants", sa.Column("checkin_streak", sa.Integer, server_default="0"))
    if not _column_exists("tenants", "last_checkin_at"):
        add_column_if_not_exists("tenants", sa.Column("last_checkin_at", sa.DateTime(timezone=True), nullable=True))
    if not _column_exists("tenants", "trial_expiry_notified"):
        add_column_if_not_exists("tenants", sa.Column("trial_expiry_notified", sa.Boolean, server_default="false"))

    # Stars balance
    if not _column_exists("tenants", "stars_balance"):
        add_column_if_not_exists("tenants", sa.Column("stars_balance", sa.Integer, server_default="0"))


def downgrade() -> None:
    cols = [
        "ai_credits_remaining", "ai_credits_reset_tokens", "ai_last_refill_at",
        "wallet_address", "is_distributor", "checkin_streak", "last_checkin_at",
        "trial_expiry_notified", "stars_balance",
    ]
    for col in cols:
        if _column_exists("tenants", col):
            op.drop_column("tenants", col)
