"""add delay_seconds to broadcasts

Revision ID: a1c3e5f7b9d1
Revises: f3a7b1c9d2e4
Create Date: 2026-07-14 08:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1c3e5f7b9d1'
down_revision: Union[str, None] = 'f3a7b1c9d2e4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('broadcasts', sa.Column('delay_seconds', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('broadcasts', 'delay_seconds')
