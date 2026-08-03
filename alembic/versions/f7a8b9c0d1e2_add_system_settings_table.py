"""add system_settings table

Revision ID: f7a8b9c0d1e2
Revises: c38582edbcc6
Create Date: 2026-07-29 06:10:00.000000

The SystemSetting model (app/models/system_setting.py) has been in use by
watermark-ad, push-notification-subscription, and admin-setup code paths, but
no migration ever created its table -- those code paths have been silently
failing with UndefinedTableError in any environment that only ran `alembic
upgrade heads` instead of `create_all`.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from migration_helpers import create_table_if_not_exists


revision: str = 'f7a8b9c0d1e2'
down_revision: Union[str, None] = 'c38582edbcc6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    create_table_if_not_exists(
        'system_settings',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('key', sa.String(length=100), nullable=False),
        sa.Column('value', sa.Text(), nullable=True),
        sa.Column('description', sa.String(length=500), nullable=True),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_system_settings_key'), 'system_settings', ['key'], unique=True)


def downgrade() -> None:
    op.drop_index(op.f('ix_system_settings_key'), table_name='system_settings')
    op.drop_table('system_settings')
