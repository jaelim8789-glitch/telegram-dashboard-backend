"""add token_valid_after column to users for per-user forced logout

Revision ID: add_token_valid_after
Revises: add_user_nickname
Create Date: 2026-08-05
"""

from alembic import op
import sqlalchemy as sa

revision = "add_token_valid_after"
down_revision = "add_user_nickname"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("token_valid_after", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "token_valid_after")
