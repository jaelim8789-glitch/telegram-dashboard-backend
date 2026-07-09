"""create group_search tables

Revision ID: e7d4c1f2a3b0
Revises: b29f4e7a1c53
Create Date: 2026-07-04 13:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e7d4c1f2a3b0"
down_revision: Union[str, None] = "b29f4e7a1c53"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "group_search_results",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("account_id", sa.String(length=36), nullable=False),
        sa.Column("keyword", sa.String(length=100), nullable=False),
        sa.Column("chat_id", sa.String(length=100), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("chat_type", sa.String(length=20), nullable=True),
        sa.Column("username", sa.String(length=100), nullable=True),
        sa.Column("participants_count", sa.Integer(), nullable=True),
        sa.Column("about", sa.Text(), nullable=True),
        sa.Column("is_joined", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_group_search_results_account_id"), "group_search_results", ["account_id"], unique=False)

    op.create_table(
        "group_join_logs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("account_id", sa.String(length=36), nullable=False),
        sa.Column("chat_id", sa.String(length=100), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("username", sa.String(length=100), nullable=True),
        sa.Column("keyword", sa.String(length=100), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_group_join_logs_account_id"), "group_join_logs", ["account_id"], unique=False)
    op.create_index(op.f("ix_group_join_logs_created_at"), "group_join_logs", ["created_at"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_group_join_logs_created_at"), table_name="group_join_logs")
    op.drop_index(op.f("ix_group_join_logs_account_id"), table_name="group_join_logs")
    op.drop_table("group_join_logs")
    op.drop_index(op.f("ix_group_search_results_account_id"), table_name="group_search_results")
    op.drop_table("group_search_results")
