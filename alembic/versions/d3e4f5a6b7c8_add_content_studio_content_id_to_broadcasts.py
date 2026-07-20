"""add broadcasts.content_studio_content_id

Revision ID: d3e4f5a6b7c8
Revises: f1a2b3c4d5e6
Create Date: 2026-07-20 17:56:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d3e4f5a6b7c8"
down_revision: Union[str, None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "broadcasts",
        sa.Column("content_studio_content_id", sa.String(length=36), nullable=True),
    )
    op.create_index(
        "ix_broadcasts_content_studio_content_id",
        "broadcasts",
        ["content_studio_content_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_broadcasts_content_studio_content_id", table_name="broadcasts")
    op.drop_column("broadcasts", "content_studio_content_id")
