"""create ai_agents, ai_chats, ai_messages tables

Revision ID: 2b68d1568159
Revises: drop_reply_macro_schedule
Create Date: 2026-07-19 20:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "2b68d1568159"
down_revision: Union[str, None] = "drop_reply_macro_schedule"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ai_agents",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("role", sa.String(length=50), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column("tools", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("is_template", sa.Boolean(), nullable=False),
        sa.Column("template_price", sa.Integer(), nullable=False),
        sa.Column("template_purchases", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("total_messages", sa.Integer(), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("exp", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_ai_agents_owner_id"), "ai_agents", ["owner_id"], unique=False)

    op.create_table(
        "ai_chats",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("agent_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["ai_agents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_ai_chats_agent_id"), "ai_chats", ["agent_id"], unique=False)
    op.create_index(op.f("ix_ai_chats_tenant_id"), "ai_chats", ["tenant_id"], unique=False)

    op.create_table(
        "ai_messages",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("chat_id", sa.String(length=36), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("tool_name", sa.String(length=50), nullable=True),
        sa.Column("tool_button_label", sa.String(length=100), nullable=True),
        sa.Column("tool_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("tokens_used", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["chat_id"], ["ai_chats.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_ai_messages_chat_id"), "ai_messages", ["chat_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_ai_messages_chat_id"), table_name="ai_messages")
    op.drop_table("ai_messages")
    op.drop_index(op.f("ix_ai_chats_tenant_id"), table_name="ai_chats")
    op.drop_index(op.f("ix_ai_chats_agent_id"), table_name="ai_chats")
    op.drop_table("ai_chats")
    op.drop_index(op.f("ix_ai_agents_owner_id"), table_name="ai_agents")
    op.drop_table("ai_agents")
