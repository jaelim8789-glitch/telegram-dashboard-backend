"""alter classification_confidence type to float

Revision ID: 55f53e2fd038
Revises: create_guest_ai_chat_logs_extended
Create Date: 2026-08-08 17:30:31.722184

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '55f53e2fd038'
down_revision: Union[str, None] = 'create_guest_ai_chat_logs_extended'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "guest_ai_chat_logs_extended",
        "classification_confidence",
        existing_type=sa.String,
        type_=sa.Float,
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "guest_ai_chat_logs_extended",
        "classification_confidence",
        existing_type=sa.Float,
        type_=sa.String,
        existing_nullable=True,
    )
