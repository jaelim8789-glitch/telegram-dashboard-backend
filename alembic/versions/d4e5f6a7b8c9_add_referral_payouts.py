"""add referral_payouts table

Revision ID: d4e5f6a7b8c9
Revises: a7b8c9d0e1f2
Create Date: 2026-07-20 01:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine import reflection


revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, None] = "a7b8c9d0e1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = reflection.Inspector.from_engine(bind)
    existing = inspector.get_table_names()

    if "referral_payouts" not in existing:
        op.create_table(
            "referral_payouts",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("referrer_id", sa.String(length=36), nullable=False),
            sa.Column("amount", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="completed"),
            sa.Column("paid_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["referrer_id"], ["tenants.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(op.f("ix_referral_payouts_referrer_id"), "referral_payouts", ["referrer_id"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = reflection.Inspector.from_engine(bind)
    existing = inspector.get_table_names()

    if "referral_payouts" in existing:
        op.drop_index(op.f("ix_referral_payouts_referrer_id"), table_name="referral_payouts")
        op.drop_table("referral_payouts")
