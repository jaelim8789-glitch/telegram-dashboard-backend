"""add guest_ai_chat_logs table

Revision ID: add_guest_ai_logs
Revises: a3f8c1d9e6b2
Create Date: 2026-08-05
"""

from alembic import op
import sqlalchemy as sa

revision = "add_guest_ai_logs"
down_revision = "a3f8c1d9e6b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "guest_ai_chat_logs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("ip", sa.String(64), nullable=False),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("reply", sa.Text, nullable=False),
        sa.Column("request_count_today", sa.Integer, server_default="1"),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index("ix_guest_ai_chat_logs_ip", "guest_ai_chat_logs", ["ip"])
    op.create_index("ix_guest_ai_chat_logs_created_at", "guest_ai_chat_logs", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_guest_ai_chat_logs_created_at", table_name="guest_ai_chat_logs")
    op.drop_index("ix_guest_ai_chat_logs_ip", table_name="guest_ai_chat_logs")
    op.drop_table("guest_ai_chat_logs")
