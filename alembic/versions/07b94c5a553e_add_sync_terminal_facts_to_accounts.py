"""add sync terminal facts to accounts

Revision ID: 07b94c5a553e
Revises: dbf35c36b076
Create Date: 2026-08-04 14:26:28.261672

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from migration_helpers import add_column_if_not_exists, drop_column_if_exists


# revision identifiers, used by Alembic.
revision: str = '07b94c5a553e'
down_revision: Union[str, None] = 'dbf35c36b076'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    add_column_if_not_exists("accounts", sa.Column("dialog_count", sa.Integer(), nullable=False, server_default=sa.text("0")))
    add_column_if_not_exists("accounts", sa.Column("message_count", sa.Integer(), nullable=False, server_default=sa.text("0")))
    add_column_if_not_exists("accounts", sa.Column("last_sync_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    drop_column_if_exists("accounts", "last_sync_at")
    drop_column_if_exists("accounts", "message_count")
    drop_column_if_exists("accounts", "dialog_count")
