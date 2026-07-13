"""add admin_audit_logs table

Revision ID: a5b6c7d8e9f0
Revises: a4b5c6d7e8f0
Create Date: 2026-07-13 09:11:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a5b6c7d8e9f0"
down_revision: Union[str, None] = "a4b5c6d7e8f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "admin_audit_logs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("admin_username", sa.String(length=100), nullable=False),
        sa.Column("action", sa.String(length=50), nullable=False),
        sa.Column("target_type", sa.String(length=50), nullable=False),
        sa.Column("target_id", sa.String(length=36), nullable=True),
        sa.Column("target_phone", sa.String(length=50), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("memo", sa.String(length=500), nullable=True),
        sa.Column("result", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_admin_audit_logs_action"), "admin_audit_logs", ["action"])
    op.create_index(op.f("ix_admin_audit_logs_target_id"), "admin_audit_logs", ["target_id"])
    op.create_index(op.f("ix_admin_audit_logs_created_at"), "admin_audit_logs", ["created_at"])


def downgrade() -> None:
    op.drop_index(op.f("ix_admin_audit_logs_created_at"), table_name="admin_audit_logs")
    op.drop_index(op.f("ix_admin_audit_logs_target_id"), table_name="admin_audit_logs")
    op.drop_index(op.f("ix_admin_audit_logs_action"), table_name="admin_audit_logs")
    op.drop_table("admin_audit_logs")
