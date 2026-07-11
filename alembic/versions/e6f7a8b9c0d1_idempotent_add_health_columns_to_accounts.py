"""Idempotent add health tracking columns to accounts table.

Revision ID: e6f7a8b9c0d1
Revises: d5a1c2b3e4f0
Create Date: 2026-07-11 17:05:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e6f7a8b9c0d1"
down_revision: Union[str, None] = "d5a1c2b3e4f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def column_exists(conn, table: str, column: str) -> bool:
    result = conn.execute(
        sa.text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = :table AND column_name = :column"
        ),
        {"table": table, "column": column},
    )
    return result.fetchone() is not None


def upgrade() -> None:
    conn = op.get_bind()

    if not column_exists(conn, "accounts", "last_error"):
        op.add_column("accounts", sa.Column("last_error", sa.Text(), nullable=True))

    if not column_exists(conn, "accounts", "last_error_at"):
        op.add_column("accounts", sa.Column("last_error_at", sa.DateTime(), nullable=True))

    if not column_exists(conn, "accounts", "last_success_at"):
        op.add_column("accounts", sa.Column("last_success_at", sa.DateTime(), nullable=True))

    if not column_exists(conn, "accounts", "health_checked_at"):
        op.add_column("accounts", sa.Column("health_checked_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("accounts", "health_checked_at")
    op.drop_column("accounts", "last_success_at")
    op.drop_column("accounts", "last_error_at")
    op.drop_column("accounts", "last_error")
