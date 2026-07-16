"""add reply_to_message_id to reply_macros

Revision ID: b5d7e9f1a2c3
Revises: f8a5d3b2c1e0
Create Date: 2026-07-16 23:15:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b5d7e9f1a2c3"
down_revision: Union[str, None] = "f8a5d3b2c1e0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "reply_macros",
        sa.Column("reply_to_message_id", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("reply_macros", "reply_to_message_id")