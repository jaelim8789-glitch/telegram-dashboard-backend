"""add referral_codes and referral_commissions tables, add referral_code_uses to tenants"""

revision: str = "add_referral_code_uses"
down_revision: str = "d3e4f5a6b7c8"
branch_labels: str | None = None
depends_on: str | None = None

from alembic import op
import sqlalchemy as sa
from alembic.idempotent import create_table_if_not_exists


def upgrade() -> None:
    op.add_column("tenants", sa.Column("referral_code_uses", sa.Integer(), nullable=False, server_default="0"))
    
    from sqlalchemy.engine import reflection
    bind = op.get_bind()
    inspector = reflection.Inspector.from_engine(bind)
    existing_tables = inspector.get_table_names()
    
    if "referral_codes" not in existing_tables:
        create_table_if_not_exists(
            "referral_codes",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("code", sa.String(30), nullable=False, unique=True, index=True),
            sa.Column("owner_id", sa.String(36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
            sa.Column("is_active", sa.Boolean(), default=True),
        )
    
    if "referral_commissions" not in existing_tables:
        create_table_if_not_exists(
            "referral_commissions",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("referrer_id", sa.String(36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("referred_id", sa.String(36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("payment_id", sa.String(36), nullable=True, index=True),
            sa.Column("amount_cents", sa.Integer(), nullable=False),
            sa.Column("rate", sa.Integer(), nullable=False, default=10),
            sa.Column("status", sa.String(20), default="pending", index=True),
            sa.Column("payment_tx_id", sa.String(100), nullable=True),
            sa.Column("paid_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        )


def downgrade() -> None:
    op.drop_table("referral_commissions")
    op.drop_table("referral_codes")
    op.drop_column("tenants", "referral_code_uses")
