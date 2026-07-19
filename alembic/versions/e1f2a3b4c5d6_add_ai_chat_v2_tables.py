"""add AI Chat v2 tables (sessions, messages, prompt templates)

Revision ID: e1f2a3b4c5d6
Revises: d180f2643056
Create Date: 2026-07-19 08:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'e1f2a3b4c5d6'
down_revision: Union[str, None] = 'd180f2643056'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── ai_chat_sessions_v2 ────────────────────────────────────────────
    op.create_table(
        'ai_chat_sessions_v2',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('tenant_id', sa.String(length=36), nullable=False),
        sa.Column('title', sa.String(length=200), server_default='New Chat', nullable=False),
        sa.Column('model', sa.String(length=50), server_default='deepseek-chat', nullable=False),
        sa.Column('tags', postgresql.JSONB(), nullable=True),
        sa.Column('metadata', postgresql.JSONB(), nullable=True),
        sa.Column('summary', sa.Text(), nullable=True),
        sa.Column('summary_updated_at', sa.DateTime(), nullable=True),
        sa.Column('message_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('total_tokens', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('source', sa.String(length=30), server_default='web_app', nullable=False),
        sa.Column('is_archived', sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_ai_chat_sessions_v2_tenant_id', 'ai_chat_sessions_v2', ['tenant_id'])
    op.create_index('ix_ai_chat_sessions_v2_created_at', 'ai_chat_sessions_v2', ['created_at'])

    # ── ai_chat_messages_v2 ────────────────────────────────────────────
    op.create_table(
        'ai_chat_messages_v2',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('session_id', sa.String(length=36), nullable=False),
        sa.Column('tenant_id', sa.String(length=36), nullable=False),
        sa.Column('role', sa.String(length=20), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('tokens_prompt', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('tokens_completion', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('latency_ms', sa.Integer(), nullable=True),
        sa.Column('model', sa.String(length=50), server_default='deepseek-chat', nullable=False),
        sa.Column('memory_context', postgresql.JSONB(), nullable=True),
        sa.Column('memory_stored', sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column('feedback_score', sa.Integer(), nullable=True),
        sa.Column('feedback_comment', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['session_id'], ['ai_chat_sessions_v2.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_ai_chat_messages_v2_session_id', 'ai_chat_messages_v2', ['session_id'])
    op.create_index('ix_ai_chat_messages_v2_tenant_id', 'ai_chat_messages_v2', ['tenant_id'])
    op.create_index('ix_ai_chat_messages_v2_created_at', 'ai_chat_messages_v2', ['created_at'])

    # ── ai_chat_prompt_templates ───────────────────────────────────────
    op.create_table(
        'ai_chat_prompt_templates',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('tenant_id', sa.String(length=36), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('description', sa.String(length=500), nullable=True),
        sa.Column('role', sa.String(length=20), server_default='system', nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('variables', postgresql.JSONB(), nullable=True),
        sa.Column('is_default', sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_ai_chat_prompt_templates_tenant_id', 'ai_chat_prompt_templates', ['tenant_id'])


def downgrade() -> None:
    op.drop_index('ix_ai_chat_prompt_templates_tenant_id', table_name='ai_chat_prompt_templates')
    op.drop_table('ai_chat_prompt_templates')

    op.drop_index('ix_ai_chat_messages_v2_created_at', table_name='ai_chat_messages_v2')
    op.drop_index('ix_ai_chat_messages_v2_tenant_id', table_name='ai_chat_messages_v2')
    op.drop_index('ix_ai_chat_messages_v2_session_id', table_name='ai_chat_messages_v2')
    op.drop_table('ai_chat_messages_v2')

    op.drop_index('ix_ai_chat_sessions_v2_created_at', table_name='ai_chat_sessions_v2')
    op.drop_index('ix_ai_chat_sessions_v2_tenant_id', table_name='ai_chat_sessions_v2')
    op.drop_table('ai_chat_sessions_v2')