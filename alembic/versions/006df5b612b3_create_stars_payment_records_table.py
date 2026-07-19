"""create stars_payment_records table

Revision ID: 006df5b612b3
Revises: 51fdc53fc518
Create Date: 2026-07-19 22:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "006df5b612b3"
down_revision: Union[str, None] = "51fdc53fc518"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "stars_payment_records",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("telegram_payment_charge_id", sa.String(length=100), nullable=False),
        sa.Column("stars_amount", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index(
        "ix_stars_payment_records_tenant_id", "stars_payment_records", ["tenant_id"]
    )
    op.create_index(
        "ix_stars_payment_records_telegram_payment_charge_id",
        "stars_payment_records",
        ["telegram_payment_charge_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_stars_payment_records_telegram_payment_charge_id", table_name="stars_payment_records")
    op.drop_index("ix_stars_payment_records_tenant_id", table_name="stars_payment_records")
    op.drop_table("stars_payment_records")
