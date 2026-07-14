"""add inline_buttons column to broadcasts

Revision ID: add_inline_buttons
Revises: merge_heads_20260713
Create Date: 2026-07-14 16:14:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "add_inline_buttons"
down_revision: Union[str, Sequence[str], None] = "merge_heads_20260713"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("broadcasts", sa.Column("inline_buttons", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("broadcasts", "inline_buttons")
