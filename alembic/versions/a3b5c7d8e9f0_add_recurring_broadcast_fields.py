"""add recurring broadcast fields to broadcasts

Revision ID: a3b5c7d8e9f0
Revises: 9d8e7c6b5a4f
Create Date: 2026-07-10 21:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from migration_helpers import create_index_if_not_exists, add_column_if_not_exists, create_foreign_key_if_not_exists


# revision identifiers, used by Alembic.
revision: str = 'a3b5c7d8e9f0'
down_revision: Union[str, None] = '9d8e7c6b5a4f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    add_column_if_not_exists(
        'broadcasts',
        sa.Column('recurring_interval_minutes', sa.Integer(), nullable=True, default=None),
    )
    add_column_if_not_exists(
        'broadcasts',
        sa.Column('cancelled_at', sa.DateTime(), nullable=True, default=None),
    )
    add_column_if_not_exists(
        'broadcasts',
        sa.Column('next_scheduled_at', sa.DateTime(), nullable=True, default=None),
    )
    add_column_if_not_exists(
        'broadcasts',
        sa.Column('parent_broadcast_id', sa.String(length=36), nullable=True, default=None),
    )
    add_column_if_not_exists(
        'broadcasts',
        sa.Column('is_recurring_paused', sa.Boolean(), nullable=False, server_default=sa.text('false')),
    )
    create_index_if_not_exists(
        op.f('ix_broadcasts_parent_broadcast_id'),
        'broadcasts',
        ['parent_broadcast_id'],
        unique=False,
    )
    create_index_if_not_exists(
        op.f('ix_broadcasts_recurring_interval_minutes'),
        'broadcasts',
        ['recurring_interval_minutes'],
        unique=False,
    )
    create_index_if_not_exists(
        op.f('ix_broadcasts_next_scheduled_at'),
        'broadcasts',
        ['next_scheduled_at'],
        unique=False,
    )
    create_foreign_key_if_not_exists(
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