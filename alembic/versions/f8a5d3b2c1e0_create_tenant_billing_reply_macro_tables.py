"""create tenant, billing, reply_macro, message_template tables

Revision ID: f8a5d3b2c1e0
Revises: e7d4c1f2a3b0
Create Date: 2026-07-09 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "f8a5d3b2c1e0"
down_revision: Union[str, None] = "e7d4c1f2a3b0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ─── Tenants (multi-tenant / 요금제) ──────────────────────────────
    op.create_table(
        "tenants",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("phone", sa.String(length=50), nullable=False),
        sa.Column("plan", sa.String(length=20), nullable=False, server_default="free"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("max_accounts", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("max_auto_reply_rules", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("max_reply_macros", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("monthly_message_limit", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("monthly_auto_reply_limit", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("cooldown_minimum_minutes", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("stripe_customer_id", sa.String(length=100), nullable=True),
        sa.Column("subscription_id", sa.String(length=100), nullable=True),
        sa.Column("subscription_status", sa.String(length=20), nullable=False, server_default="inactive"),
        sa.Column("payment_ref", sa.String(length=100), nullable=True),
        sa.Column("billing_period_start", sa.DateTime(), nullable=True),
        sa.Column("billing_period_end", sa.DateTime(), nullable=True),
        sa.Column("stars_balance", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("can_broadcast", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("can_schedule", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("can_attach_images", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("can_export_data", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("can_use_api", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("referred_by", sa.String(length=36), nullable=True),
        sa.Column("referral_code", sa.String(length=20), nullable=False),
        sa.Column("referral_earnings", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("phone"),
        sa.UniqueConstraint("referral_code"),
    )

    # ─── Payment Records (USDT) ─────────────────────────────────────
    op.create_table(
        "payment_records",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tx_id", sa.String(length=100), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=True),
        sa.Column("from_address", sa.String(length=100), nullable=False),
        sa.Column("amount_usdt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("plan", sa.String(length=20), nullable=True),
        sa.Column("billing", sa.String(length=10), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("api_key_id", sa.String(length=36), nullable=True),
        sa.Column("block_timestamp", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tx_id"),
    )
    op.create_index(op.f("ix_payment_records_tenant_id"), "payment_records", ["tenant_id"], unique=False)

    # ─── Usage Records ──────────────────────────────────────────────
    op.create_table(
        "usage_records",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("action", sa.String(length=50), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("recorded_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_usage_records_tenant_id"), "usage_records", ["tenant_id"], unique=False)
    op.create_index(op.f("ix_usage_records_action"), "usage_records", ["action"], unique=False)
    op.create_index(op.f("ix_usage_records_recorded_at"), "usage_records", ["recorded_at"], unique=False)

    # ─── Leads (CRM) ────────────────────────────────────────────────
    op.create_table(
        "leads",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("account_id", sa.String(length=36), nullable=False),
        sa.Column("telegram_user_id", sa.String(length=100), nullable=False),
        sa.Column("telegram_username", sa.String(length=100), nullable=True),
        sa.Column("first_name", sa.String(length=100), nullable=True),
        sa.Column("last_name", sa.String(length=100), nullable=True),
        sa.Column("phone", sa.String(length=50), nullable=True),
        sa.Column("source_chat_id", sa.String(length=100), nullable=False),
        sa.Column("source_rule_id", sa.String(length=36), nullable=True),
        sa.Column("total_messages", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_interaction", sa.DateTime(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("tags", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_leads_tenant_id"), "leads", ["tenant_id"], unique=False)
    op.create_index(op.f("ix_leads_account_id"), "leads", ["account_id"], unique=False)

    # ─── Reply Macros ───────────────────────────────────────────────
    op.create_table(
        "reply_macros",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("account_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("target_chats", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("message_content", sa.Text(), nullable=False),
        sa.Column("media_path", sa.String(length=500), nullable=True),
        sa.Column("schedule_type", sa.String(length=20), nullable=False, server_default="interval"),
        sa.Column("interval_hours", sa.Integer(), nullable=False, server_default="24"),
        sa.Column("fixed_time", sa.String(length=5), nullable=True),
        sa.Column("max_sends_per_day", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("last_sent_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_reply_macros_account_id"), "reply_macros", ["account_id"], unique=False)

    op.create_table(
        "reply_macro_logs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("macro_id", sa.String(length=36), nullable=False),
        sa.Column("account_id", sa.String(length=36), nullable=False),
        sa.Column("target_chat_id", sa.String(length=100), nullable=False),
        sa.Column("message_sent", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["macro_id"], ["reply_macros.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_reply_macro_logs_macro_id"), "reply_macro_logs", ["macro_id"], unique=False)
    op.create_index(op.f("ix_reply_macro_logs_account_id"), "reply_macro_logs", ["account_id"], unique=False)
    op.create_index(op.f("ix_reply_macro_logs_status"), "reply_macro_logs", ["status"], unique=False)
    op.create_index(op.f("ix_reply_macro_logs_created_at"), "reply_macro_logs", ["created_at"], unique=False)

    # ─── Message Templates ──────────────────────────────────────────
    op.create_table(
        "message_templates",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("category", sa.String(length=50), nullable=False, server_default="general"),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("variables", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("is_favorite", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("use_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_message_templates_tenant_id"), "message_templates", ["tenant_id"], unique=False)

    # ─── Follow-up Rules ────────────────────────────────────────────
    op.create_table(
        "follow_up_rules",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("account_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("trigger_delay_hours", sa.Integer(), nullable=False, server_default="24"),
        sa.Column("message_content", sa.Text(), nullable=False),
        sa.Column("match_keyword", sa.String(length=200), nullable=True),
        sa.Column("max_sends_per_day", sa.Integer(), nullable=False, server_default="50"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_follow_up_rules_tenant_id"), "follow_up_rules", ["tenant_id"], unique=False)
    op.create_index(op.f("ix_follow_up_rules_account_id"), "follow_up_rules", ["account_id"], unique=False)

    # ─── Team Members ──────────────────────────────────────────────
    op.create_table(
        "team_members",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("username", sa.String(length=100), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False, server_default="operator"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_login", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_team_members_tenant_id"), "team_members", ["tenant_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_team_members_tenant_id"), table_name="team_members")
    op.drop_table("team_members")

    op.drop_index(op.f("ix_follow_up_rules_account_id"), table_name="follow_up_rules")
    op.drop_index(op.f("ix_follow_up_rules_tenant_id"), table_name="follow_up_rules")
    op.drop_table("follow_up_rules")

    op.drop_index(op.f("ix_message_templates_tenant_id"), table_name="message_templates")
    op.drop_table("message_templates")

    op.drop_index(op.f("ix_reply_macro_logs_created_at"), table_name="reply_macro_logs")
    op.drop_index(op.f("ix_reply_macro_logs_status"), table_name="reply_macro_logs")
    op.drop_index(op.f("ix_reply_macro_logs_account_id"), table_name="reply_macro_logs")
    op.drop_index(op.f("ix_reply_macro_logs_macro_id"), table_name="reply_macro_logs")
    op.drop_table("reply_macro_logs")

    op.drop_index(op.f("ix_reply_macros_account_id"), table_name="reply_macros")
    op.drop_table("reply_macros")

    op.drop_index(op.f("ix_leads_account_id"), table_name="leads")
    op.drop_index(op.f("ix_leads_tenant_id"), table_name="leads")
    op.drop_table("leads")

    op.drop_index(op.f("ix_usage_records_recorded_at"), table_name="usage_records")
    op.drop_index(op.f("ix_usage_records_action"), table_name="usage_records")
    op.drop_index(op.f("ix_usage_records_tenant_id"), table_name="usage_records")
    op.drop_table("usage_records")

    op.drop_index(op.f("ix_payment_records_tenant_id"), table_name="payment_records")
    op.drop_table("payment_records")

    op.drop_table("tenants")