"""merge all heads

Revision ID: c38582edbcc6
Revises: add_missing_indexes_20260722, b3d9f1a2c4e6, e26f59ef08e8
Create Date: 2026-07-28 08:34:56.319786

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c38582edbcc6'
down_revision: Union[str, None] = ('add_missing_indexes_20260722', 'b3d9f1a2c4e6', 'e26f59ef08e8')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
