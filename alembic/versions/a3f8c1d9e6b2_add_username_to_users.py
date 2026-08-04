"""add username to users

Revision ID: a3f8c1d9e6b2
Revises: 07b94c5a553e
Create Date: 2026-08-04 21:05:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from migration_helpers import add_column_if_not_exists, drop_column_if_exists


# revision identifiers, used by Alembic.
revision: str = 'a3f8c1d9e6b2'
down_revision: Union[str, None] = '07b94c5a553e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Nullable + unique: phone-based users never set this, only accounts
    # created via the id/password guest signup do.
    add_column_if_not_exists("users", sa.Column("username", sa.String(64), nullable=True))
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_username ON users (username) WHERE username IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_users_username")
    drop_column_if_exists("users", "username")
