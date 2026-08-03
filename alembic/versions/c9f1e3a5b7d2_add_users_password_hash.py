"""add users.password_hash column

Revision ID: c9f1e3a5b7d2
Revises: 1c4d2d63fd1b
Create Date: 2026-07-22 01:35:00

"""
from alembic import op
import sqlalchemy as sa
from migration_helpers import add_column_if_not_exists

revision = "c9f1e3a5b7d2"
down_revision = "1c4d2d63fd1b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_not_exists("users", sa.Column("password_hash", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "password_hash")
