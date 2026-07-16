"""Merge broadcast_schedule and team+ai_drafts heads

Revision ID: 8fd32dca8ec4
Revises: 528ff4bece8b, add_broadcast_schedule
Create Date: 2026-07-17 04:33:36.530431

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8fd32dca8ec4'
down_revision: Union[str, None] = ('528ff4bece8b', 'add_broadcast_schedule')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
