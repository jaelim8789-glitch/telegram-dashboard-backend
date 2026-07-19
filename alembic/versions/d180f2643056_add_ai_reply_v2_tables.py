"""add AI Reply v2 tables (personas, conversations, suggestions_v2)

Revision ID: d180f2643056
Revises: add_ai_platform_tables
Create Date: 2026-07-19 08:20:00.000000

Renamed from a1b2c3d4e5f6, which collided with the pre-existing
a1b2c3d4e5f6_add_tenant_id_to_accounts.py (an unrelated, already-applied
migration from earlier in the chain) - Alembic refused to run with a
duplicate revision ID present ("Multiple head revisions").
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'd180f2643056'
down_revision: Union[str, None] = 'add_ai_platform_tables'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── ai_reply_personas ─────────────────────────────────────────────
    op.create_table(
        'ai_reply_personas',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('tenant_id', sa.String(length=36), nullable=False),
        sa.Column('account_id', sa.String(length=36), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('is_active', sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column('tone', sa.String(length=20), server_default='professional', nullable=False),
        sa.Column('style', postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column('business_info', postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_ai_reply_personas_tenant_id', 'ai_reply_personas', ['tenant_id'])
    op.create_index('ix_ai_reply_personas_account_id', 'ai_reply_personas', ['account_id'])

    # ── ai_reply_conversations ────────────────────────────────────────
    op.create_table(
        'ai_reply_conversations',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('tenant_id', sa.String(length=36), nullable=False),
        sa.Column('account_id', sa.String(length=36), nullable=False),
        sa.Column('chat_id', sa.String(length=100), nullable=False),
        sa.Column('chat_title', sa.String(length=200), nullable=True),
        sa.Column('messages', postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column('summary', sa.Text(), nullable=True),
        sa.Column('summary_updated_at', sa.DateTime(), nullable=True),
        sa.Column('message_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('last_message_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_ai_reply_conversations_tenant_id', 'ai_reply_conversations', ['tenant_id'])
    op.create_index('ix_ai_reply_conversations_account_id', 'ai_reply_conversations', ['account_id'])
    op.create_index('ix_ai_reply_conversations_chat_id', 'ai_reply_conversations', ['chat_id'])

    # ── ai_reply_suggestions_v2 ───────────────────────────────────────
    op.create_table(
        'ai_reply_suggestions_v2',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('tenant_id', sa.String(length=36), nullable=False),
        sa.Column('account_id', sa.String(length=36), nullable=False),
        sa.Column('chat_id', sa.String(length=100), nullable=False),
        sa.Column('chat_title', sa.String(length=200), nullable=True),
        sa.Column('user_id', sa.String(length=100), nullable=False),
        sa.Column('user_name', sa.String(length=100), nullable=True),
        sa.Column('incoming_message', sa.Text(), nullable=False),
        sa.Column('suggestions', postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column('context', postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column('status', sa.String(length=20), server_default='pending', nullable=False),
        sa.Column('auto_reply_enabled', sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column('auto_reply_sent', sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column('auto_reply_sent_at', sa.DateTime(), nullable=True),
        sa.Column('reviewed_by', sa.String(length=100), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(), nullable=True),
        sa.Column('selected_suggestion', sa.String(length=20), nullable=True),
        sa.Column('custom_reply', sa.Text(), nullable=True),
        sa.Column('feedback', postgresql.JSONB(), nullable=True),
        sa.Column('response_time_ms', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_ai_reply_suggestions_v2_tenant_id', 'ai_reply_suggestions_v2', ['tenant_id'])
    op.create_index('ix_ai_reply_suggestions_v2_account_id', 'ai_reply_suggestions_v2', ['account_id'])
    op.create_index('ix_ai_reply_suggestions_v2_chat_id', 'ai_reply_suggestions_v2', ['chat_id'])
    op.create_index('ix_ai_reply_suggestions_v2_created_at', 'ai_reply_suggestions_v2', ['created_at'])
    op.create_index('ix_ai_reply_suggestions_v2_status', 'ai_reply_suggestions_v2', ['status'])


def downgrade() -> None:
    op.drop_index('ix_ai_reply_suggestions_v2_status', table_name='ai_reply_suggestions_v2')
    op.drop_index('ix_ai_reply_suggestions_v2_created_at', table_name='ai_reply_suggestions_v2')
    op.drop_index('ix_ai_reply_suggestions_v2_chat_id', table_name='ai_reply_suggestions_v2')
    op.drop_index('ix_ai_reply_suggestions_v2_account_id', table_name='ai_reply_suggestions_v2')
    op.drop_index('ix_ai_reply_suggestions_v2_tenant_id', table_name='ai_reply_suggestions_v2')
    op.drop_table('ai_reply_suggestions_v2')

    op.drop_index('ix_ai_reply_conversations_chat_id', table_name='ai_reply_conversations')
    op.drop_index('ix_ai_reply_conversations_account_id', table_name='ai_reply_conversations')
    op.drop_index('ix_ai_reply_conversations_tenant_id', table_name='ai_reply_conversations')
    op.drop_table('ai_reply_conversations')

    op.drop_index('ix_ai_reply_personas_account_id', table_name='ai_reply_personas')
    op.drop_index('ix_ai_reply_personas_tenant_id', table_name='ai_reply_personas')
    op.drop_table('ai_reply_personas')