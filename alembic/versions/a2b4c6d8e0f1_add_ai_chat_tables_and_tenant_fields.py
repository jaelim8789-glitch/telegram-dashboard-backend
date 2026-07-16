"""add ai_chat_messages table and tenant AI Chat fields

Revision ID: a2b4c6d8e0f1
Revises: merge_folders_and_reply_macro_heads
Create Date: 2026-07-17 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a2b4c6d8e0f1'
down_revision: Union[str, None] = 'merge_folders_and_reply_macro_heads'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('tenants', sa.Column('monthly_ai_chat_limit', sa.Integer(), server_default='20', nullable=False))
    op.add_column('tenants', sa.Column('ai_chat_credit_balance', sa.Integer(), server_default='0', nullable=False))

    op.create_table(
        'ai_chat_messages',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('tenant_id', sa.String(length=36), nullable=False),
        sa.Column('telegram_user_id', sa.String(length=100), nullable=False),
        sa.Column('role', sa.String(length=20), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_ai_chat_messages_tenant_id', 'ai_chat_messages', ['tenant_id'])
    op.create_index('ix_ai_chat_messages_telegram_user_id', 'ai_chat_messages', ['telegram_user_id'])
    op.create_index('ix_ai_chat_messages_created_at', 'ai_chat_messages', ['created_at'])


def downgrade() -> None:
    op.drop_index('ix_ai_chat_messages_created_at', table_name='ai_chat_messages')
    op.drop_index('ix_ai_chat_messages_telegram_user_id', table_name='ai_chat_messages')
    op.drop_index('ix_ai_chat_messages_tenant_id', table_name='ai_chat_messages')
    op.drop_table('ai_chat_messages')

    op.drop_column('tenants', 'ai_chat_credit_balance')
    op.drop_column('tenants', 'monthly_ai_chat_limit')
