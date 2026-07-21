"""merge is_distributor field with mainline

Revision ID: 1c4d2d63fd1b
Revises: f1e2d3c4b5a6, b3f8e2a1c4d5
Create Date: 2026-07-21 20:02:28.800372

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1c4d2d63fd1b'
down_revision: Union[str, None] = ('f1e2d3c4b5a6', 'b3f8e2a1c4d5')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
