"""merge_team_and_ai_drafts

Revision ID: 528ff4bece8b
Revises: d4e6f8a0b2c4, a7b8c9d0e1f2
Create Date: 2026-07-17 02:13:15.724653

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '528ff4bece8b'
down_revision: Union[str, None] = ('d4e6f8a0b2c4', 'a7b8c9d0e1f2')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
