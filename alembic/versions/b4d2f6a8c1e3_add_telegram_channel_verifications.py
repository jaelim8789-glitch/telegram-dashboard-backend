"""add telegram_channel_verifications table

Revision ID: b4d2f6a8c1e3
Revises: 2f1d3c4b5a6e
Create Date: 2026-07-12 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b4d2f6a8c1e3'
down_revision: Union[str, None] = '2f1d3c4b5a6e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'telegram_channel_verifications',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('telegram_user_id', sa.BigInteger(), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('linked_at', sa.DateTime(), nullable=True),
        sa.Column('verified_at', sa.DateTime(), nullable=True),
        sa.Column('consumed_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('telegram_channel_verifications')
