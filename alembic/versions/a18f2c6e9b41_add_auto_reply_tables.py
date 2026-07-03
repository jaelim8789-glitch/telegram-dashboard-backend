"""add auto reply tables

Revision ID: a18f2c6e9b41
Revises: c9f33665cd67
Create Date: 2026-07-04 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a18f2c6e9b41'
down_revision: Union[str, None] = 'c9f33665cd67'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('accounts', sa.Column('auto_reply_enabled', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.alter_column('accounts', 'auto_reply_enabled', server_default=None)

    op.create_table(
        'auto_reply_rules',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('account_id', sa.String(length=36), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('match_type', sa.String(length=20), nullable=False),
        sa.Column('match_value', sa.Text(), nullable=False),
        sa.Column('reply_content', sa.Text(), nullable=False),
        sa.Column('cooldown_hours', sa.Integer(), nullable=False),
        sa.Column('max_replies_per_day', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_auto_reply_rules_account_id'), 'auto_reply_rules', ['account_id'], unique=False)

    op.create_table(
        'auto_reply_logs',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('rule_id', sa.String(length=36), nullable=False),
        sa.Column('account_id', sa.String(length=36), nullable=False),
        sa.Column('chat_id', sa.String(length=100), nullable=False),
        sa.Column('user_id', sa.String(length=100), nullable=False),
        sa.Column('user_name', sa.String(length=100), nullable=True),
        sa.Column('trigger_message', sa.Text(), nullable=False),
        sa.Column('reply_sent', sa.Text(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['rule_id'], ['auto_reply_rules.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_auto_reply_logs_rule_id'), 'auto_reply_logs', ['rule_id'], unique=False)
    op.create_index(op.f('ix_auto_reply_logs_account_id'), 'auto_reply_logs', ['account_id'], unique=False)
    op.create_index(op.f('ix_auto_reply_logs_status'), 'auto_reply_logs', ['status'], unique=False)
    op.create_index(op.f('ix_auto_reply_logs_created_at'), 'auto_reply_logs', ['created_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_auto_reply_logs_created_at'), table_name='auto_reply_logs')
    op.drop_index(op.f('ix_auto_reply_logs_status'), table_name='auto_reply_logs')
    op.drop_index(op.f('ix_auto_reply_logs_account_id'), table_name='auto_reply_logs')
    op.drop_index(op.f('ix_auto_reply_logs_rule_id'), table_name='auto_reply_logs')
    op.drop_table('auto_reply_logs')

    op.drop_index(op.f('ix_auto_reply_rules_account_id'), table_name='auto_reply_rules')
    op.drop_table('auto_reply_rules')

    op.drop_column('accounts', 'auto_reply_enabled')
