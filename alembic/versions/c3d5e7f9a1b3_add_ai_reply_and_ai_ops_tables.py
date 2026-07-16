"""add ai reply/ops tables and account ai_fallback_reply_enabled

Revision ID: c3d5e7f9a1b3
Revises: a2b4c6d8e0f1
Create Date: 2026-07-17 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3d5e7f9a1b3'
down_revision: Union[str, None] = 'a2b4c6d8e0f1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'accounts',
        sa.Column('ai_fallback_reply_enabled', sa.Boolean(), server_default=sa.false(), nullable=False),
    )

    op.create_table(
        'auto_reply_suggestions',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('account_id', sa.String(length=36), nullable=False),
        sa.Column('chat_id', sa.String(length=100), nullable=False),
        sa.Column('user_id', sa.String(length=100), nullable=False),
        sa.Column('user_name', sa.String(length=100), nullable=True),
        sa.Column('trigger_message', sa.Text(), nullable=False),
        sa.Column('suggested_reply', sa.Text(), nullable=False),
        sa.Column('reviewed', sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_auto_reply_suggestions_account_id', 'auto_reply_suggestions', ['account_id'])
    op.create_index('ix_auto_reply_suggestions_created_at', 'auto_reply_suggestions', ['created_at'])

    op.create_table(
        'ai_ops_reports',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('report', sa.Text(), nullable=False),
        sa.Column('anomalies_json', sa.Text(), server_default='[]', nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_ai_ops_reports_created_at', 'ai_ops_reports', ['created_at'])


def downgrade() -> None:
    op.drop_index('ix_ai_ops_reports_created_at', table_name='ai_ops_reports')
    op.drop_table('ai_ops_reports')

    op.drop_index('ix_auto_reply_suggestions_created_at', table_name='auto_reply_suggestions')
    op.drop_index('ix_auto_reply_suggestions_account_id', table_name='auto_reply_suggestions')
    op.drop_table('auto_reply_suggestions')

    op.drop_column('accounts', 'ai_fallback_reply_enabled')
