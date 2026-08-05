"""add nickname column to users

Revision ID: add_user_nickname
Revises: add_guest_ai_logs
Create Date: 2026-08-05
"""

from alembic import op
import sqlalchemy as sa

revision = "add_user_nickname"
down_revision = "add_guest_ai_logs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("nickname", sa.String(50), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "nickname")
