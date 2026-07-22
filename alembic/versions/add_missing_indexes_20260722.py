"""Add missing indexes identified by index audit.

Audit findings (2026-07-22):
  message_logs:      (tenant_id, created_at) 복합 — 통계/필터링
  broadcasts:        (account_id, status) 복합 — 대시보드 목록
  accounts:          (tenant_id, status) 복합 — 테넌트별 계정 현황
  leads:             (tenant_id, is_active) — 활성 리드 필터
  auto_reply_logs:   (account_id, status, created_at) — 로그 조회
  reply_macro_logs:  (account_id, status, created_at) — 로그 조회
  usage_records:     (tenant_id, action, recorded_at) — 사용량 통계
  ai_chat_logs:      (tenant_id, session_id) — 세션 메시지 로드
  campaigns:         (tenant_id, status) — 캠페인 목록
  message_templates: (tenant_id, category) — 템플릿 필터
  team_members:      (tenant_id, role) — 팀 관리
  follow_up_rules:   (tenant_id, account_id, is_active) — 활성 규칙 조회
  join_queue_items:  (account_id, status) — 큐 처리
Revision ID: add_missing_indexes_20260722
Revises: (previous head — update manually)
Create Date: 2026-07-22
"""
from alembic import op
import sqlalchemy as sa


revision = "add_missing_indexes_20260722"
down_revision = None  # SET THIS to actual head before applying
branch_labels = None
depends_on = None


def upgrade() -> None:
    # message_logs
    op.create_index("ix_message_logs_tenant_created", "message_logs", ["tenant_id", "created_at"])
    op.create_index("ix_message_logs_recipient_source", "message_logs", ["recipient", "source"])

    # broadcasts
    op.create_index("ix_broadcasts_account_status", "broadcasts", ["account_id", "status"])

    # accounts
    op.create_index("ix_accounts_tenant_status", "accounts", ["tenant_id", "status"])

    # leads
    op.create_index("ix_leads_tenant_active", "leads", ["tenant_id", "is_active"])

    # auto_reply_logs
    op.create_index("ix_auto_reply_logs_account_status_created",
                    "auto_reply_logs", ["account_id", "status", "created_at"])

    # reply_macro_logs
    op.create_index("ix_reply_macro_logs_account_status_created",
                    "reply_macro_logs", ["account_id", "status", "created_at"])

    # usage_records
    op.create_index("ix_usage_records_tenant_action_recorded",
                    "usage_records", ["tenant_id", "action", "recorded_at"])

    # ai_chat_logs
    op.create_index("ix_ai_chat_logs_tenant_session",
                    "ai_chat_logs", ["tenant_id", "session_id"])

    # campaigns
    op.create_index("ix_campaigns_tenant_status", "campaigns", ["tenant_id", "status"])

    # message_templates
    op.create_index("ix_message_templates_tenant_category",
                    "message_templates", ["tenant_id", "category"])

    # team_members
    op.create_index("ix_team_members_tenant_role", "team_members", ["tenant_id", "role"])

    # follow_up_rules
    op.create_index("ix_follow_up_rules_tenant_account_active",
                    "follow_up_rules", ["tenant_id", "account_id", "is_active"])

    # join_queue_items
    op.create_index("ix_join_queue_items_account_status",
                    "join_queue_items", ["account_id", "status"])

    # BroadcastScheduleEntry
    op.create_index("ix_broadcast_schedule_entries_tenant_status",
                    "broadcast_schedule_entries", ["tenant_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_broadcast_schedule_entries_tenant_status")
    op.drop_index("ix_join_queue_items_account_status")
    op.drop_index("ix_follow_up_rules_tenant_account_active")
    op.drop_index("ix_team_members_tenant_role")
    op.drop_index("ix_message_templates_tenant_category")
    op.drop_index("ix_campaigns_tenant_status")
    op.drop_index("ix_ai_chat_logs_tenant_session")
    op.drop_index("ix_usage_records_tenant_action_recorded")
    op.drop_index("ix_reply_macro_logs_account_status_created")
    op.drop_index("ix_auto_reply_logs_account_status_created")
    op.drop_index("ix_leads_tenant_active")
    op.drop_index("ix_accounts_tenant_status")
    op.drop_index("ix_broadcasts_account_status")
    op.drop_index("ix_message_logs_recipient_source")
    op.drop_index("ix_message_logs_tenant_created")
