"""add referral_codes, referral_commissions tables and referred_by FK

Revision ID: a7b8c9d0e1f2
Revises: b1d2e3f4a5b6
Create Date: 2026-07-20 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine import reflection


revision: str = "a7b8c9d0e1f2"
down_revision: Union[str, None] = "b1d2e3f4a5b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def table_has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    inspector = reflection.Inspector.from_engine(bind)
    try:
        cols = [c["name"] for c in inspector.get_columns(table)]
        return column in cols
    except Exception:
        return False


def upgrade() -> None:
    bind = op.get_bind()
    inspector = reflection.Inspector.from_engine(bind)
    existing_tables = inspector.get_table_names()

    if "referral_codes" not in existing_tables:
        op.create_table(
            "referral_codes",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("code", sa.String(length=30), nullable=False),
            sa.Column("owner_id", sa.String(length=36), nullable=False),
            sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["owner_id"], ["tenants.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(op.f("ix_referral_codes_code"), "referral_codes", ["code"], unique=True)
        op.create_index(op.f("ix_referral_codes_owner_id"), "referral_codes", ["owner_id"], unique=False)

    if "referral_commissions" not in existing_tables:
        op.create_table(
            "referral_commissions",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("referrer_id", sa.String(length=36), nullable=False),
            sa.Column("referred_user_id", sa.String(length=36), nullable=False),
            sa.Column("source_payment_id", sa.String(length=36), nullable=False),
            sa.Column("source_type", sa.String(length=10), nullable=False),
            sa.Column("amount", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("commission_rate", sa.Float(), nullable=False, server_default="0.0"),
            sa.Column("commission_amount", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
            sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["referrer_id"], ["tenants.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["referred_user_id"], ["tenants.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(op.f("ix_referral_commissions_referrer_id"), "referral_commissions", ["referrer_id"], unique=False)
        op.create_index(op.f("ix_referral_commissions_referred_user_id"), "referral_commissions", ["referred_user_id"], unique=False)

    if "tenants" in existing_tables:
        if not table_has_column("tenants", "referred_by"):
            op.add_column("tenants", sa.Column("referred_by", sa.String(length=36), nullable=True))
        if not table_has_column("tenants", "referral_code"):
            op.add_column("tenants", sa.Column("referral_code", sa.String(length=20), nullable=True, unique=True))
        if not table_has_column("tenants", "referral_earnings"):
            op.add_column("tenants", sa.Column("referral_earnings", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    if table_has_column("tenants", "referral_earnings"):
        op.drop_column("tenants", "referral_earnings")
    if table_has_column("tenants", "referral_code"):
        op.drop_column("tenants", "referral_code")
    if table_has_column("tenants", "referred_by"):
        op.drop_column("tenants", "referred_by")

    bind = op.get_bind()
    inspector = reflection.Inspector.from_engine(bind)
    existing_tables = inspector.get_table_names()

    if "referral_commissions" in existing_tables:
        op.drop_index(op.f("ix_referral_commissions_referred_user_id"), table_name="referral_commissions")
        op.drop_index(op.f("ix_referral_commissions_referrer_id"), table_name="referral_commissions")
        op.drop_table("referral_commissions")
    if "referral_codes" in existing_tables:
        op.drop_index(op.f("ix_referral_codes_owner_id"), table_name="referral_codes")
        op.drop_index(op.f("ix_referral_codes_code"), table_name="referral_codes")
        op.drop_table("referral_codes")
