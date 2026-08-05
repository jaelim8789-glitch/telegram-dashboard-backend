"""AI Learning system: guest feedback column, KB document versioning, kb_candidates table

Adds the columns/table the AI Learning feature (Knowledge Candidates, KB
document versioning, guest thumbs-up feedback) needs -- these were added to
the ORM models without a migration, so none of them exist in the actual
database yet.

Revision ID: add_ai_learning_tables
Revises: add_token_valid_after
Create Date: 2026-08-06
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from migration_helpers import add_column_if_not_exists, create_table_if_not_exists

revision = "add_ai_learning_tables"
down_revision = "add_token_valid_after"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Guest AI chat thumbs-up/down feedback
    add_column_if_not_exists("guest_ai_chat_logs", sa.Column("thumbs_up", sa.Boolean(), nullable=True))

    # Knowledge Base document versioning
    add_column_if_not_exists("kb_documents", sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
    add_column_if_not_exists("kb_documents", sa.Column("parent_id", postgresql.UUID(as_uuid=False), nullable=True))
    add_column_if_not_exists("kb_documents", sa.Column("is_latest", sa.Boolean(), nullable=False, server_default=sa.true()))
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing_fks = [fk["name"] for fk in insp.get_foreign_keys("kb_documents")]
    if "fk_kb_documents_parent_id" not in existing_fks:
        op.create_foreign_key(
            "fk_kb_documents_parent_id", "kb_documents", "kb_documents", ["parent_id"], ["id"],
        )

    # Knowledge Candidates (learning approval queue)
    create_table_if_not_exists(
        "kb_candidates",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("feedback_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("feedback_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("model_name", sa.String(length=100), nullable=False, server_default="unknown"),
        sa.Column("tokens_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("response_time_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ai_version", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("prompt_version", sa.String(length=50), nullable=False, server_default="v1"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("approval_reason", sa.String(length=100), nullable=True),
        sa.Column("approval_comment", sa.Text(), nullable=True),
        sa.Column("reviewed_by", sa.String(length=36), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()")),
    )
    bind = op.get_bind()
    existing_indexes = [ix["name"] for ix in sa.inspect(bind).get_indexes("kb_candidates")] if bind.dialect.has_table(bind, "kb_candidates") else []
    if "ix_kb_candidates_tenant_id" not in existing_indexes:
        op.create_index("ix_kb_candidates_tenant_id", "kb_candidates", ["tenant_id"])
    if "ix_kb_candidates_status" not in existing_indexes:
        op.create_index("ix_kb_candidates_status", "kb_candidates", ["status"])


def downgrade() -> None:
    op.drop_index("ix_kb_candidates_status", table_name="kb_candidates")
    op.drop_index("ix_kb_candidates_tenant_id", table_name="kb_candidates")
    op.drop_table("kb_candidates")
    op.drop_constraint("fk_kb_documents_parent_id", "kb_documents", type_="foreignkey")
    op.drop_column("kb_documents", "is_latest")
    op.drop_column("kb_documents", "parent_id")
    op.drop_column("kb_documents", "version")
    op.drop_column("guest_ai_chat_logs", "thumbs_up")
