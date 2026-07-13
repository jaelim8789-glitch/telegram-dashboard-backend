"""Merge heads: broadcast_mode chain + billing chain

Revision ID: merge_heads_20260713
Revises: 8a1b2c3d4e5f, a6b7c8d9e0f1
Create Date: 2026-07-13 14:31:00.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = "merge_heads_20260713"
down_revision: Union[str, Sequence[str], None] = ("8a1b2c3d4e5f", "a6b7c8d9e0f1")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
