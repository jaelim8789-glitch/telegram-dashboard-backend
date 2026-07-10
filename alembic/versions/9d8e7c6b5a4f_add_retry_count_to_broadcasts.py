"""add retry_count to broadcasts

Revision ID: 9d8e7c6b5a4f
Revises: 846f21ea3264
Create Date: 2026-07-10 15:45:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9d8e7c6b5a4f'
down_revision: Union[str, None] = '846f21ea3264'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'broadcasts',
        sa.Column('retry_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
    )


def downgrade() -> None:
    op.drop_column('broadcasts', 'retry_count')