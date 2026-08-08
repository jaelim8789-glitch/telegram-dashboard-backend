"""create guest_ai_chat_logs_extended table for guest AI chat analytics

Revision ID: create_guest_ai_chat_logs_extended
Revises: create_blacklisted_entities
Create Date: 2026-08-08

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "create_guest_ai_chat_logs_extended"
down_revision = "create_blacklisted_entities"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "guest_ai_chat_logs_extended",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, default=sa.text("gen_random_uuid()")),
        sa.Column("ip", sa.String, nullable=False),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("reply", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.text("now()")),
        sa.Column("thumbs_up", sa.Boolean, nullable=True),
        sa.Column("device_id", sa.String, nullable=False),
        sa.Column("session_id", UUID(as_uuid=True), nullable=False),
        sa.Column("turn_number", sa.Integer, nullable=False, server_default="1"),
        sa.Column("confidence", sa.String, nullable=True),
        sa.Column("primary_category", sa.String, nullable=True),
        sa.Column("secondary_category", sa.String, nullable=True),
        sa.Column("classification_confidence", sa.String, nullable=True),
        sa.Column("converted_user_id", UUID(as_uuid=True), nullable=True),
        sa.Column("conversion_tracked", sa.Boolean, nullable=False, server_default="false"),
    )
    op.create_index("ix_guest_ai_chat_logs_extended_ip", "guest_ai_chat_logs_extended", ["ip"])
    op.create_index("ix_guest_ai_chat_logs_extended_device_id", "guest_ai_chat_logs_extended", ["device_id"])
    op.create_index("ix_guest_ai_chat_logs_extended_session_id", "guest_ai_chat_logs_extended", ["session_id"])


def downgrade() -> None:
    op.drop_index("ix_guest_ai_chat_logs_extended_session_id", table_name="guest_ai_chat_logs_extended")
    op.drop_index("ix_guest_ai_chat_logs_extended_device_id", table_name="guest_ai_chat_logs_extended")
    op.drop_index("ix_guest_ai_chat_logs_extended_ip", table_name="guest_ai_chat_logs_extended")
    op.drop_table("guest_ai_chat_logs_extended")