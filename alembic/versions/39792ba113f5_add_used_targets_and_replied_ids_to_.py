"""add used_targets and replied_user_id/msg_id to reply_macro

Revision ID: 39792ba113f5
Revises: 93d331972067
Create Date: 2026-07-19 13:35:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '39792ba113f5'
down_revision: Union[str, None] = '93d331972067'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('reply_macros', sa.Column('used_targets', sa.Text(), server_default='[]', nullable=False))
    op.add_column('reply_macro_logs', sa.Column('replied_user_id', sa.String(length=100), nullable=True))
    op.add_column('reply_macro_logs', sa.Column('replied_msg_id', sa.Integer(), nullable=True))
    op.create_index(op.f('ix_reply_macro_logs_replied_user_id'), 'reply_macro_logs', ['replied_user_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_reply_macro_logs_replied_user_id'), table_name='reply_macro_logs')
    op.drop_column('reply_macro_logs', 'replied_msg_id')
    op.drop_column('reply_macro_logs', 'replied_user_id')
    op.drop_column('reply_macros', 'used_targets')
