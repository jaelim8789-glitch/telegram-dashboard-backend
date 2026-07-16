"""add ai_broadcast_drafts table

Revision ID: d4e6f8a0b2c4
Revises: c3d5e7f9a1b3
Create Date: 2026-07-17 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4e6f8a0b2c4'
down_revision: Union[str, None] = 'c3d5e7f9a1b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'ai_broadcast_drafts',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('prompt', sa.Text(), nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('recommended_chat_ids_json', sa.Text(), server_default='[]', nullable=False),
        sa.Column('reasoning', sa.Text(), server_default='', nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_ai_broadcast_drafts_created_at', 'ai_broadcast_drafts', ['created_at'])


def downgrade() -> None:
    op.drop_index('ix_ai_broadcast_drafts_created_at', table_name='ai_broadcast_drafts')
    op.drop_table('ai_broadcast_drafts')
