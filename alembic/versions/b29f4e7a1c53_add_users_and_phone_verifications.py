"""add users and phone_verifications tables

Revision ID: b29f4e7a1c53
Revises: a18f2c6e9b41
Create Date: 2026-07-04 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b29f4e7a1c53'
down_revision: Union[str, None] = 'a18f2c6e9b41'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'users',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('phone', sa.String(length=50), nullable=False),
        sa.Column('api_key_hash', sa.String(length=128), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('last_login', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_users_phone'), 'users', ['phone'], unique=True)
    op.create_index(op.f('ix_users_api_key_hash'), 'users', ['api_key_hash'], unique=True)

    op.create_table(
        'phone_verifications',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('phone', sa.String(length=50), nullable=False),
        sa.Column('code_hash', sa.String(length=64), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_phone_verifications_phone'), 'phone_verifications', ['phone'], unique=True)


def downgrade() -> None:
    op.drop_index(op.f('ix_phone_verifications_phone'), table_name='phone_verifications')
    op.drop_table('phone_verifications')

    op.drop_index(op.f('ix_users_api_key_hash'), table_name='users')
    op.drop_index(op.f('ix_users_phone'), table_name='users')
    op.drop_table('users')
