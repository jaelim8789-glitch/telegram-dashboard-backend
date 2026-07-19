"""drop schedule columns from reply_macros

Removes the old interval/fixed dispatch schedule fields
(schedule_type, interval_hours, fixed_time, max_sends_per_day,
reply_to_message_id) now that reply macros are driven only by the
random-reply endpoint.

This migration also merges the existing parallel heads into a single
head so `alembic upgrade heads` resolves cleanly.

Revision ID: drop_reply_macro_schedule
Revises: a6b7c8d9e0f1, a7b8c9d0e1f2, add_ai_platform_tables,
add_broadcast_schedule, add_user_sessions, b2c3d4e5f6a7,
b5d7e9f1a2c3, d8e9f0a1b2c3, e7f8a1b2c3d4,
merge_folders_and_reply_macro_heads
Create Date: 2026-07-19 18:44:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "drop_reply_macro_schedule"
down_revision: Union[str, Sequence[str], None] = (
    "a6b7c8d9e0f1",
    "a7b8c9d0e1f2",
    "add_ai_platform_tables",
    "add_broadcast_schedule",
    "add_user_sessions",
    "b2c3d4e5f6a7",
    "b5d7e9f1a2c3",
    "d8e9f0a1b2c3",
    "e7f8a1b2c3d4",
    "merge_folders_and_reply_macro_heads",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Idempotent drops: on some deployments these columns may already be
    # absent, which would crash a straight drop_column(). Guard each one.
    conn = op.get_bind()
    existing_columns = {col["name"] for col in sa.inspect(conn).get_columns("reply_macros")}

    for col in (
        "schedule_type",
        "interval_hours",
        "fixed_time",
        "max_sends_per_day",
        "reply_to_message_id",
    ):
        if col in existing_columns:
            op.drop_column("reply_macros", col)
            existing_columns.discard(col)


def downgrade() -> None:
    op.add_column("reply_macros", sa.Column("schedule_type", sa.String(length=20), nullable=False, server_default="interval"))
    op.add_column("reply_macros", sa.Column("interval_hours", sa.Integer(), nullable=False, server_default="24"))
    op.add_column("reply_macros", sa.Column("fixed_time", sa.String(length=5), nullable=True))
    op.add_column("reply_macros", sa.Column("max_sends_per_day", sa.Integer(), nullable=False, server_default="10"))
    op.add_column("reply_macros", sa.Column("reply_to_message_id", sa.Integer(), nullable=True))
