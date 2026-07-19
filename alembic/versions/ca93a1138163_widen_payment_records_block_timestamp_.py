"""widen payment_records.block_timestamp to bigint (TronGrid returns ms, exceeds int32)

Revision ID: ca93a1138163
Revises: d9e8f7c6b5a4
Create Date: 2026-07-19 18:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "ca93a1138163"
down_revision: Union[str, None] = "d9e8f7c6b5a4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "payment_records",
        "block_timestamp",
        existing_type=sa.Integer(),
        type_=sa.BigInteger(),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "payment_records",
        "block_timestamp",
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        existing_nullable=False,
    )
