"""add users.password_hash column

Revision ID: a1b2c3d4e5f6
Revises: 1c4d2d63fd1b
Create Date: 2026-07-22 01:35:00

"""
from alembic import op
import sqlalchemy as sa

revision = "a1b2c3d4e5f6"
down_revision = "1c4d2d63fd1b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("password_hash", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "password_hash")
