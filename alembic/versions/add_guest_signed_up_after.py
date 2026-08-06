"""Add signed_up_after to guest_ai_chat_logs (funnel analytics)

Tracks which guest AI IPs later sign up, so we can measure which guest
experiences convert.

Revision ID: add_guest_signed_up_after
Revises: add_guest_ai_logs
Create Date: 2026-08-06
"""

from alembic import op
import sqlalchemy as sa

from migration_helpers import add_column_if_not_exists

revision = "add_guest_signed_up_after"
down_revision = "add_guest_ai_logs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_not_exists("guest_ai_chat_logs", sa.Column("signed_up_after", sa.Boolean(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.has_table(bind, "guest_ai_chat_logs"):
        insp = sa.inspect(bind)
        cols = [c["name"] for c in insp.get_columns("guest_ai_chat_logs")]
        if "signed_up_after" in cols:
            op.drop_column("guest_ai_chat_logs", "signed_up_after")
