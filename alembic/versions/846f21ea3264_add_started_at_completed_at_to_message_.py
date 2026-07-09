"""add started_at completed_at to message_logs

Revision ID: 846f21ea3264
Revises: b2c3d4e5f6a7
Create Date: 2026-07-10 06:02:49.162802

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '846f21ea3264'
down_revision: Union[str, None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('message_logs', sa.Column('started_at', sa.DateTime(), nullable=True))
    op.add_column('message_logs', sa.Column('completed_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column('message_logs', 'completed_at')
    op.drop_column('message_logs', 'started_at')