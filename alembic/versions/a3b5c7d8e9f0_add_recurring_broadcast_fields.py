"""add recurring broadcast fields to broadcasts

Revision ID: a3b5c7d8e9f0
Revises: 9d8e7c6b5a4f
Create Date: 2026-07-10 21:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3b5c7d8e9f0'
down_revision: Union[str, None] = '9d8e7c6b5a4f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'broadcasts',
        sa.Column('recurring_interval_minutes', sa.Integer(), nullable=True, default=None),
    )
    op.add_column(
        'broadcasts',
        sa.Column('cancelled_at', sa.DateTime(), nullable=True, default=None),
    )
    op.add_column(
        'broadcasts',
        sa.Column('next_scheduled_at', sa.DateTime(), nullable=True, default=None),
    )
    op.add_column(
        'broadcasts',
        sa.Column('parent_broadcast_id', sa.String(length=36), nullable=True, default=None),
    )
    op.add_column(
        'broadcasts',
        sa.Column('is_recurring_paused', sa.Boolean(), nullable=False, server_default=sa.text('false')),
    )
    op.create_index(
        op.f('ix_broadcasts_parent_broadcast_id'),
        'broadcasts',
        ['parent_broadcast_id'],
        unique=False,
    )
    op.create_index(
        op.f('ix_broadcasts_recurring_interval_minutes'),
        'broadcasts',
        ['recurring_interval_minutes'],
        unique=False,
    )
    op.create_index(
        op.f('ix_broadcasts_next_scheduled_at'),
        'broadcasts',
        ['next_scheduled_at'],
        unique=False,
    )
    op.create_foreign_key(
        'fk_broadcasts_parent_broadcast_id',
        'broadcasts',
        'broadcasts',
        ['parent_broadcast_id'],
        ['id'],
        ondelete='SET NULL',
    )


def downgrade() -> None:
    op.drop_constraint('fk_broadcasts_parent_broadcast_id', 'broadcasts', type_='foreignkey')
    op.drop_index(op.f('ix_broadcasts_next_scheduled_at'), table_name='broadcasts')
    op.drop_index(op.f('ix_broadcasts_recurring_interval_minutes'), table_name='broadcasts')
    op.drop_index(op.f('ix_broadcasts_parent_broadcast_id'), table_name='broadcasts')
    op.drop_column('broadcasts', 'is_recurring_paused')
    op.drop_column('broadcasts', 'parent_broadcast_id')
    op.drop_column('broadcasts', 'next_scheduled_at')
    op.drop_column('broadcasts', 'cancelled_at')
    op.drop_column('broadcasts', 'recurring_interval_minutes')