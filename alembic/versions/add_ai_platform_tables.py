"""create AI platform tables (chat/reply/broadcast assistants, ops reports, usage, plan limits)

Revision ID: add_ai_platform_tables
Revises: add_campaign_fields_broadcasts
Create Date: 2026-07-19 00:00:00.000000

app/models/ai.py (added in 8962dcb "expand TeleMon AI platform with
Graphiti-backed memory and new endpoints") declares 6 new tables with no
matching migration - same missing-migration pattern already hit twice
during the previous deploy (campaigns, broadcasts.campaign_id). Adding
proactively before deploy this time instead of discovering it via a
crash loop.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "add_ai_platform_tables"
down_revision: Union[str, Sequence[str], None] = "add_campaign_fields_broadcasts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ai_chat_logs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("metadata", sa.JSON, nullable=True),
        sa.Column("tokens_used", sa.Integer, nullable=False, server_default="0"),
        sa.Column("model", sa.String(50), nullable=True, server_default="deepseek-chat"),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_ai_chat_logs_tenant_id", "ai_chat_logs", ["tenant_id"])
    op.create_index("ix_ai_chat_logs_session_id", "ai_chat_logs", ["session_id"])
    op.create_index("ix_ai_chat_logs_created_at", "ai_chat_logs", ["created_at"])

    op.create_table(
        "ai_reply_assistant_logs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("account_id", sa.String(36), nullable=False),
        sa.Column("chat_id", sa.String(100), nullable=False),
        sa.Column("chat_title", sa.String(200), nullable=True),
        sa.Column("incoming_message", sa.Text, nullable=False),
        sa.Column("suggested_reply", sa.Text, nullable=False),
        sa.Column("confidence", sa.Float, nullable=False, server_default="0"),
        sa.Column("context_summary", sa.Text, nullable=True),
        sa.Column("was_sent", sa.Boolean, nullable=False, server_default="0"),
        sa.Column("sent_at", sa.DateTime, nullable=True),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_ai_reply_assistant_logs_tenant_id", "ai_reply_assistant_logs", ["tenant_id"])
    op.create_index("ix_ai_reply_assistant_logs_account_id", "ai_reply_assistant_logs", ["account_id"])
    op.create_index("ix_ai_reply_assistant_logs_created_at", "ai_reply_assistant_logs", ["created_at"])

    op.create_table(
        "ai_broadcast_assistant_logs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("account_id", sa.String(36), nullable=True),
        sa.Column("purpose", sa.String(500), nullable=False),
        sa.Column("target_description", sa.Text, nullable=True),
        sa.Column("generated_message", sa.Text, nullable=False),
        sa.Column("variant_a", sa.Text, nullable=True),
        sa.Column("variant_b", sa.Text, nullable=True),
        sa.Column("tone", sa.String(50), nullable=True),
        sa.Column("language", sa.String(10), nullable=True, server_default="ko"),
        sa.Column("was_sent", sa.Boolean, nullable=False, server_default="0"),
        sa.Column("sent_at", sa.DateTime, nullable=True),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_ai_broadcast_assistant_logs_tenant_id", "ai_broadcast_assistant_logs", ["tenant_id"])
    op.create_index("ix_ai_broadcast_assistant_logs_created_at", "ai_broadcast_assistant_logs", ["created_at"])

    op.create_table(
        "ai_operations_reports",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("report_type", sa.String(50), nullable=False),
        sa.Column("period_start", sa.DateTime, nullable=False),
        sa.Column("period_end", sa.DateTime, nullable=False),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("sections", sa.JSON, nullable=True),
        sa.Column("insights", sa.JSON, nullable=True),
        sa.Column("recommendations", sa.JSON, nullable=True),
        sa.Column("metrics", sa.JSON, nullable=True),
        sa.Column("tokens_used", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_ai_operations_reports_tenant_id", "ai_operations_reports", ["tenant_id"])
    op.create_index("ix_ai_operations_reports_created_at", "ai_operations_reports", ["created_at"])

    op.create_table(
        "ai_usage_records",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("feature", sa.String(50), nullable=False),
        sa.Column("tokens_used", sa.Integer, nullable=False, server_default="0"),
        sa.Column("requests_count", sa.Integer, nullable=False, server_default="1"),
        sa.Column("cost_credits", sa.Float, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_ai_usage_records_tenant_id", "ai_usage_records", ["tenant_id"])
    op.create_index("ix_ai_usage_records_feature", "ai_usage_records", ["feature"])
    op.create_index("ix_ai_usage_records_created_at", "ai_usage_records", ["created_at"])

    op.create_table(
        "ai_plan_limits",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("plan", sa.String(50), nullable=False),
        sa.Column("feature", sa.String(50), nullable=False),
        sa.Column("max_requests_per_day", sa.Integer, nullable=False, server_default="0"),
        sa.Column("max_tokens_per_day", sa.Integer, nullable=False, server_default="0"),
        sa.Column("max_credits_per_month", sa.Float, nullable=False, server_default="0"),
        sa.Column("is_enabled", sa.Boolean, nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("ai_plan_limits")
    op.drop_index("ix_ai_usage_records_created_at", table_name="ai_usage_records")
    op.drop_index("ix_ai_usage_records_feature", table_name="ai_usage_records")
    op.drop_index("ix_ai_usage_records_tenant_id", table_name="ai_usage_records")
    op.drop_table("ai_usage_records")
    op.drop_index("ix_ai_operations_reports_created_at", table_name="ai_operations_reports")
    op.drop_index("ix_ai_operations_reports_tenant_id", table_name="ai_operations_reports")
    op.drop_table("ai_operations_reports")
    op.drop_index("ix_ai_broadcast_assistant_logs_created_at", table_name="ai_broadcast_assistant_logs")
    op.drop_index("ix_ai_broadcast_assistant_logs_tenant_id", table_name="ai_broadcast_assistant_logs")
    op.drop_table("ai_broadcast_assistant_logs")
    op.drop_index("ix_ai_reply_assistant_logs_created_at", table_name="ai_reply_assistant_logs")
    op.drop_index("ix_ai_reply_assistant_logs_account_id", table_name="ai_reply_assistant_logs")
    op.drop_index("ix_ai_reply_assistant_logs_tenant_id", table_name="ai_reply_assistant_logs")
    op.drop_table("ai_reply_assistant_logs")
    op.drop_index("ix_ai_chat_logs_created_at", table_name="ai_chat_logs")
    op.drop_index("ix_ai_chat_logs_session_id", table_name="ai_chat_logs")
    op.drop_index("ix_ai_chat_logs_tenant_id", table_name="ai_chat_logs")
    op.drop_table("ai_chat_logs")
