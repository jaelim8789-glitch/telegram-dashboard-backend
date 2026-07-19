"""add telegram_id, telegram_username, telegram_photo_url to users

Revision ID: d9e8f7c6b5a4
Revises: 39792ba113f5
Create Date: 2026-07-19 14:35:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d9e8f7c6b5a4"
down_revision: Union[str, None] = "39792ba113f5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("telegram_id", sa.Integer(), nullable=True))
    op.create_index(op.f("ix_users_telegram_id"), "users", ["telegram_id"], unique=True)
    op.add_column("users", sa.Column("telegram_username", sa.String(255), nullable=True))
    op.add_column("users", sa.Column("telegram_photo_url", sa.String(512), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "telegram_photo_url")
    op.drop_column("users", "telegram_username")
    op.drop_index(op.f("ix_users_telegram_id"), table_name="users")
    op.drop_column("users", "telegram_id")
