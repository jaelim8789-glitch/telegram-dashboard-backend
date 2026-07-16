"""create folders table

Revision ID: b3c7f1a9d2e4
Revises: merge_session_and_inline_buttons
Create Date: 2026-07-16 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b3c7f1a9d2e4"
down_revision: Union[str, None] = "merge_session_and_inline_buttons"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "folders",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("account_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("color", sa.String(length=20), nullable=False, server_default="#6366f1"),
        sa.Column("icon", sa.String(length=50), nullable=False, server_default="folder"),
        sa.Column("group_ids", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("parent_id", sa.String(length=36), nullable=True),
        sa.Column("is_collapsed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_smart", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("smart_type", sa.String(length=50), nullable=True),
        sa.Column("smart_params", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["parent_id"], ["folders.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_folders_account_id"), "folders", ["account_id"], unique=False)
    op.create_index(op.f("ix_folders_parent_id"), "folders", ["parent_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_folders_parent_id"), table_name="folders")
    op.drop_index(op.f("ix_folders_account_id"), table_name="folders")
    op.drop_table("folders")
