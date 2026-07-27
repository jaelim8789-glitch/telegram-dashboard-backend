"""Add missing indexes identified by index audit.

Audit findings (2026-07-22):
  message_logs:      (tenant_id, created_at)   /
  broadcasts:        (account_id, status)    
  accounts:          (tenant_id, status)     
  leads:             (tenant_id, is_active)    
  auto_reply_logs:   (account_id, status, created_at)   
  reply_macro_logs:  (account_id, status, created_at)   
  usage_records:     (tenant_id, action, recorded_at)   
  ai_chat_logs:      (tenant_id, session_id)    
  campaigns:         (tenant_id, status)   
  message_templates: (tenant_id, category)   
  team_members:      (tenant_id, role)   
  follow_up_rules:   (tenant_id, account_id, is_active)    
  join_queue_items:  (account_id, status)   
Revision ID: add_missing_indexes_20260722
Revises: (previous head  update manually)
Create Date: 2026-07-22
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine import reflection


revision = "add_missing_indexes_20260722"
down_revision = None  # Will be set dynamically
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    inspector = reflection.Inspector.from_engine(bind)
    try:
        cols = [c["name"] for c in inspector.get_columns(table)]
        return column in cols
    except Exception:
        return False


def upgrade() -> None:
    # message_logs
    if _has_column("message_logs", "tenant_id"):
        op.create_index("ix_message_logs_tenant_created", "message_logs", ["tenant_id", "created_at"])
    op.create_index("ix_message_logs_recipient_source", "message_logs", ["recipient", "source"])

    # broadcasts
    op.create_index("ix_broadcasts_account_status", "broadcasts", ["account_id", "status"])

    # accounts
    if _has_column("accounts", "tenant_id"):
        op.create_index("ix_accounts_tenant_status", "accounts", ["tenant_id", "status"])

    # leads
    if _has_column("leads", "tenant_id"):
        op.create_index("ix_leads_tenant_active", "leads", ["tenant_id", "is_active"])

    # auto_reply_logs
    op.create_index("ix_auto_reply_logs_account_status_created",
                    "auto_reply_logs", ["account_id", "status", "created_at"])

    # reply_macro_logs
    op.create_index("ix_reply_macro_logs_account_status_created",
                    "reply_macro_logs", ["account_id", "status", "created_at"])

    # usage_records
    if _has_column("usage_records", "tenant_id"):
        op.create_index("ix_usage_records_tenant_action_recorded",
                        "usage_records", ["tenant_id", "action", "recorded_at"])

    # ai_chat_logs
    if _has_column("ai_chat_logs", "tenant_id"):
        op.create_index("ix_ai_chat_logs_tenant_session",
                        "ai_chat_logs", ["tenant_id", "session_id"])

    # campaigns
    if _has_column("campaigns", "tenant_id"):
        op.create_index("ix_campaigns_tenant_status", "campaigns", ["tenant_id", "status"])

    # message_templates
    if _has_column("message_templates", "tenant_id"):
        op.create_index("ix_message_templates_tenant_category",
                        "message_templates", ["tenant_id", "category"])

    # team_members
    if _has_column("team_members", "tenant_id"):
        op.create_index("ix_team_members_tenant_role", "team_members", ["tenant_id", "role"])

    # follow_up_rules
    op.create_index("ix_follow_up_rules_tenant_account_active",
                    "follow_up_rules", ["tenant_id", "account_id", "is_active"])

    # join_queue_items
    op.create_index("ix_join_queue_items_account_status",
                    "join_queue_items", ["account_id", "status"])

    # BroadcastScheduleEntry
    if _has_column("broadcast_schedule_entries", "tenant_id"):
        op.create_index("ix_broadcast_schedule_entries_tenant_status",
                        "broadcast_schedule_entries", ["tenant_id", "status"])


def downgrade() -> None:
    _drop_index_if_exists("ix_broadcast_schedule_entries_tenant_status")
    _drop_index_if_exists("ix_join_queue_items_account_status")
    _drop_index_if_exists("ix_follow_up_rules_tenant_account_active")
    _drop_index_if_exists("ix_team_members_tenant_role")
    _drop_index_if_exists("ix_message_templates_tenant_category")
    _drop_index_if_exists("ix_campaigns_tenant_status")
    _drop_index_if_exists("ix_ai_chat_logs_tenant_session")
    _drop_index_if_exists("ix_usage_records_tenant_action_recorded")
    _drop_index_if_exists("ix_reply_macro_logs_account_status_created")
    _drop_index_if_exists("ix_auto_reply_logs_account_status_created")
    _drop_index_if_exists("ix_leads_tenant_active")
    _drop_index_if_exists("ix_accounts_tenant_status")
    _drop_index_if_exists("ix_broadcasts_account_status")
    _drop_index_if_exists("ix_message_logs_recipient_source")
    _drop_index_if_exists("ix_message_logs_tenant_created")


def _drop_index_if_exists(name: str) -> None:
    try:
        op.drop_index(name)
    except Exception:
        pass
